import copy
import os
import numpy as np
import torch
import torch.distributed
from pathlib import Path
from modules.base.base_trainer import BaseTrainer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm
from modules.utils import get_topic_list, get_project_root, get_topic_dist, load_sparse, load_dataset_df, \
    read_json, NPMI, compute_coherence, write_to_file, save_topic_info, MetricTracker, load_batch_data, word_tokenize


class NCTrainer(BaseTrainer):
    """
    Trainer class
    """
    def __init__(self, model, config, data_loader, **kwargs):
        super().__init__(model, config)
        self.config = config
        self.data_loader = data_loader
        self.train_loader = data_loader.train_loader
        self.entropy_constraint = config.get("entropy_constraint", False)
        self.calculate_entropy = config.get("calculate_entropy", self.entropy_constraint)
        self.alpha = config.get("alpha", 0.001)
        self.len_epoch = len(self.train_loader)
        self.valid_loader = data_loader.valid_loader
        self.do_validation = self.valid_loader is not None
        self.log_step = int(np.sqrt(self.train_loader.batch_size))
        self.train_metrics = MetricTracker(*self.metric_funcs, writer=self.writer)
        self.valid_metrics = MetricTracker(*self.metric_funcs, writer=self.writer)
        self.model, self.optimizer, self.train_loader, self.lr_scheduler = self.accelerator.prepare(
            self.model, self.optimizer, self.train_loader, self.lr_scheduler)

    def run_model(self, batch_dict, model=None, multi_gpu=True):
        """
        run model with the batch data
        :param multi_gpu: default use multi-gpu training
        :param batch_dict: the dictionary of data with format like {"news": Tensor(), "label": Tensor()}
        :param model: by default we use the self model
        :return: the output of running, label used for evaluation, and loss item
        """
        batch_dict = load_batch_data(batch_dict, self.device, multi_gpu)
        output = model(batch_dict) if model is not None else self.model(batch_dict)
        loss = self.criterion(output["predicted"], batch_dict["label"])
        out_dict = {"label": batch_dict["label"], "loss": loss, "predict": output["predicted"]}
        if self.entropy_constraint:
            loss += self.alpha * output["entropy"]
        if self.calculate_entropy:
            out_dict.update({"attention_weight": output["attention"], "entropy": output["entropy"]})
        return out_dict

    def update_metrics(self, metrics=None, out_dict=None, predicts=None, labels=None):
        if predicts is not None and labels is not None:
            for met in self.metric_funcs:  # run metric functions
                metrics.update(met.__name__, met(predicts, labels), n=len(labels))
        else:
            n = len(out_dict["label"])
            metrics.update("loss", out_dict["loss"].item(), n=n)  # update metrix
            if self.calculate_entropy:
                metrics.update("doc_entropy", out_dict["entropy"].item() / n, n=n)

    def _train_epoch(self, epoch):
        """
        Training logic for an epoch
        :param epoch: Integer, current training epoch.
        :return: A log that contains average loss and metric in this epoch.
        """
        self.model.train()
        self.train_metrics.reset()
        bar = tqdm(enumerate(self.train_loader), total=len(self.train_loader))
        labels, predicts = [], []
        for batch_idx, batch_dict in bar:
            self.optimizer.zero_grad()  # setup gradient to zero
            out_dict = self.run_model(batch_dict, self.model)  # run model

            self.accelerator.backward(out_dict["loss"])  # backpropagation
            self.optimizer.step()  # gradient descent
            self.writer.set_step((epoch - 1) * self.len_epoch + batch_idx, "train")
            self.update_metrics(self.train_metrics, out_dict)
            labels.extend(out_dict["label"].cpu().tolist())
            predicts.extend(torch.argmax(out_dict["predict"], dim=1).cpu().tolist())
            if batch_idx % self.log_step == 0:  # set bar
                bar.set_description(f"Train Epoch: {epoch} Loss: {out_dict['loss'].item()}")
            if batch_idx == self.len_epoch:
                break
        self.update_metrics(self.train_metrics, predicts=predicts, labels=labels)
        log = self.train_metrics.result()
        if self.do_validation:
            log.update(self.evaluate(self.valid_loader, self.model, epoch))  # update validation log

        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
        return log

    def evaluate(self, loader, model, epoch=0, prefix="val"):
        model.eval()
        self.valid_metrics.reset()
        labels, predicts = [], []
        with torch.no_grad():
            for batch_idx, batch_dict in tqdm(enumerate(loader), total=len(loader)):
                out_dict = self.run_model(batch_dict, model, multi_gpu=False)
                self.writer.set_step((epoch - 1) * len(loader) + batch_idx, "evaluate")
                self.update_metrics(self.valid_metrics, out_dict)
                labels.extend(out_dict["label"].cpu().tolist())
                predicts.extend(torch.argmax(out_dict["predict"], dim=1).cpu().tolist())
        self.update_metrics(self.valid_metrics, predicts=predicts, labels=labels)
        for name, p in model.named_parameters():  # add histogram of model parameters to the tensorboard
            self.writer.add_histogram(name, p, bins='auto')
        log = {f"{prefix}_{k}": v for k, v in self.valid_metrics.result().items()}  # return log with prefix
        return log

    def topic_evaluation(self, model=None, data_loader=None, extra_str=None):
        """
        evaluate the topic quality of the BATM model using the topic coherence
        :param model: best model chosen from the training process
        :param data_loader: should have a word_dict variable
        :param extra_str: extra string to add to the file name
        :return: topic quality result of the best model
        """
        if model is None:
            model = self.model
        if data_loader is None:
            data_loader = self.data_loader
        topic_evaluation_method = self.config.get("topic_evaluation_method", None)
        saved_name = f"topics_{self.config.seed}_{self.config.head_num}"
        if extra_str is not None:
            saved_name += f"_{extra_str}"
        topic_path = Path(self.config.model_dir, saved_name)
        reverse_dict = {v: k for k, v in data_loader.word_dict.items()}
        topic_dist = get_topic_dist(model, data_loader, self.config.get("topic_variant", "base"))
        self.model = self.model.to(self.device)
        top_n, methods = self.config.get("top_n", 10), self.config.get("coherence_method", "c_npmi")
        post_word_dict_dir = self.config.get("post_word_dict_dir", None)
        topic_dists = {"original": topic_dist}
        if post_word_dict_dir is not None:
            for path in os.scandir(post_word_dict_dir):
                if not path.name.endswith(".json"):
                    continue
                post_word_dict = read_json(path)
                removed_index = [v for k, v in data_loader.word_dict.items() if k not in post_word_dict]
                topic_dist_copy = copy.deepcopy(topic_dist)  # copy original topical distribution
                topic_dist_copy[:, removed_index] = 0  # set removed terms to 0
                topic_dists[path.name.replace(".json", "")] = topic_dist_copy
        topic_result, topic_scores = {}, None
        if torch.distributed.is_initialized():
            model = model.module
        for key, dist in topic_dists.items():
            topic_list = get_topic_list(dist, top_n, reverse_dict)  # convert to tokens list
            ref_data_path = self.config.get("ref_data_path", Path(get_project_root()) / "dataset/data/MIND15.csv")
            if self.config.get("save_topic_info", False) and self.accelerator.is_main_process:  # save topic info
                os.makedirs(topic_path, exist_ok=True)
                write_to_file(os.path.join(topic_path, "topic_list.txt"), [" ".join(topics) for topics in topic_list])
            if "fast_eval" in topic_evaluation_method:
                ref_texts = load_sparse(ref_data_path)
                scorer = NPMI((ref_texts > 0).astype(int))
                topic_index = [[data_loader.word_dict[word] - 1 for word in topic] for topic in topic_list]
                topic_scores = {f"{key}_c_npmi": scorer.compute_npmi(topics=topic_index, n=top_n)}
            if "slow_eval" in topic_evaluation_method:
                dataset_name = self.config.get("dataset_name", "MIND15")
                tokenized_method = self.config.get("tokenized_method", "use_tokenize")
                ref_df, _ = load_dataset_df(dataset_name, data_path=ref_data_path, tokenized_method=tokenized_method)
                ref_texts = [word_tokenize(doc, tokenized_method) for doc in ref_df["data"].values]
                topic_scores = {f"{key}_m": compute_coherence(topic_list, ref_texts, m, top_n) for m in methods}
            if ("slow_eval" in topic_evaluation_method or "fast_eval" in topic_evaluation_method) and topic_scores:
                if self.config.get("save_topic_info", False) and self.accelerator.is_main_process:
                    # avoid duplicated saving
                    sort_score = self.config.get("sort_score", True)
                    topic_result.update(save_topic_info(topic_path, topic_list, topic_scores, sort_score))
                else:
                    topic_result.update({m: np.round(np.mean(c), 4) for m, c in topic_scores.items()})
            if "w2v_sim" in topic_evaluation_method:  # compute word embedding similarity of top-10 words for each topic
                embeddings = model.embedding_layer.embedding.weight.cpu().detach().numpy()
                count = model.head_num * top_n * (top_n - 1) / 2
                topic_index = [[data_loader.word_dict[word] for word in topic] for topic in topic_list]
                w2v_sim = sum([np.sum(np.triu(cosine_similarity(embeddings[i]), 1)) for i in topic_index]) / count
                topic_result.update({f"{key}_w2v_sim": np.round(w2v_sim, 4)})
        if not len(topic_result):
            raise ValueError("No correct topic evaluation method is specified!")
        return topic_result
