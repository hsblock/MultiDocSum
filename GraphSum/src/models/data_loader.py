import json
import glob
import gc
from collections import namedtuple

import numpy as np
import torch

from utils.logging import logger


def chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i: i + n]


class DataBatch(object):

    def __init__(self, args, data=None, pad_idx=None, device=None, is_test=False):
        self.args = args
        self.n_heads = self.args.n_heads
        self.max_para_num = self.args.max_para_num
        self.max_para_len = self.args.max_para_len
        self.max_tgt_len = self.args.max_tgt_len
        self.pad_idx = pad_idx

        if data is not None:
            # src, tgt_ids, label_ids, tgt_str, graph
            self.batch_size = len(data)

            enc_input, dec_input, tgt_label, label_weight = self.process_batch(data, device)

            setattr(self, 'enc_input', enc_input)
            setattr(self, 'dec_input', dec_input)
            setattr(self, 'tgt_label', tgt_label)
            setattr(self, 'label_weight', label_weight)

            if is_test:
                tgt_str = [inst[3] for inst in data]
                setattr(self, 'tgt_str', tgt_str)

    def __len__(self):
        return self.batch_size

    def process_batch(self, data, device):
        src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias, \
            src_sents_self_attn_bias, graph_attn_bias = self._pad_src_batch_data(
                insts=[inst[0] for inst in data],
                graphs=[inst[4] for inst in data],
                device=device
            )

        tgt_words, tgt_pos, tgt_self_attn_bias = self._pad_tgt_batch_data(
            insts=[inst[1] for inst in data],
            device=device
        )

        tgt_label, label_weight = self._pad_label_batch_data(
            insts=[inst[2] for inst in data],
            device=device
        )

        src_words_self_attn_bias = src_words_self_attn_bias.unsqueeze(2).unsqueeze(3) \
            .expand(-1, -1, self.n_heads, self.max_para_len, -1)
        src_words_self_attn_bias.requires_grad = False

        src_sents_self_attn_bias = src_sents_self_attn_bias.unsqueeze(1).unsqueeze(2) \
            .expand(-1, self.n_heads, self.max_para_num, -1)
        src_sents_self_attn_bias.requires_grad = False

        graph_attn_bias = graph_attn_bias.unsqueeze(1).expand(-1, self.n_heads, -1, -1)
        graph_attn_bias.requires_grad = False

        tgt_self_attn_bias = tgt_self_attn_bias.unsqueeze(1).expand(-1, self.n_heads, -1, -1)
        tgt_self_attn_bias.requires_grad = False

        tgt_src_words_attn_bias = src_words_self_attn_bias[:, :, :, 0].unsqueeze(3) \
            .expand(-1, -1, -1, self.max_tgt_len, -1)
        tgt_src_words_attn_bias.requires_grad = False

        tgt_src_sents_attn_bias = src_sents_self_attn_bias[:, :, 0].unsqueeze(2) \
            .expand(-1, -1, self.max_tgt_len, -1)
        tgt_src_sents_attn_bias.requires_grad = False

        src_words = src_words.view(-1, self.max_para_num, self.max_para_len)
        src_words_pos = src_words_pos.view(-1, self.max_para_num, self.max_para_len)
        src_sents_pos = src_sents_pos.view(-1, self.max_para_num)
        tgt_words = tgt_words.view(-1, self.max_tgt_len)
        tgt_pos = tgt_pos.view(-1, self.max_tgt_len)
        tgt_label = tgt_label.view(-1, 1)
        label_weight = label_weight.view(-1, 1)

        enc_input = (src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias,
                     src_sents_self_attn_bias, graph_attn_bias)
        dec_input = (tgt_words, tgt_pos, tgt_self_attn_bias, tgt_src_words_attn_bias,
                     tgt_src_sents_attn_bias, graph_attn_bias)

        return enc_input, dec_input, tgt_label, label_weight

    def _pad(self, data, height, width, pad_id):
        # input => [height, width]
        rtn_data = [d + [pad_id] * (width - len(d)) for d in data]
        rtn_data = rtn_data + [[pad_id] * width] * (height - len(data))

        return rtn_data

    def _pad_src_batch_data(self, insts, graphs, device):
        # [batch_size, max_n_blocks, max_n_tokens]
        src_words = [self._pad(inst, self.max_para_num, self.max_para_len, self.pad_idx)
                     for inst in insts]
        src_words = torch.tensor(src_words, dtype=torch.int64, device=device)

        # [batch_size, max_n_blocks, max_n_tokens]
        src_words_pos = [[list(range(0, len(para))) + [0] * (self.max_para_len - len(para))
                          for para in inst] +
                         [[0] * self.max_para_len] * (self.max_para_num - len(inst))
                         for inst in insts]
        src_words_pos = torch.tensor(src_words_pos, dtype=torch.int64, device=device)

        # [batch_size, max_n_blocks]
        src_sents_pos = [list(range(0, len(inst))) + [0] * (self.max_para_num - len(inst))
                         for inst in insts]
        src_sents_pos = torch.tensor(src_sents_pos, dtype=torch.int64, device=device)

        # 在 paddings 上不计算 attention
        # [batch_size, max_n_blocks, max_n_tokens]
        src_words_self_attn_bias = [[[0.0] * len(para) + [-1e18] * (self.max_para_len - len(para))
                                     for para in inst] +
                                    [[-1e18] * self.max_para_len] * (self.max_para_num - len(inst))
                                    for inst in insts]
        src_words_self_attn_bias = torch.tensor(src_words_self_attn_bias, dtype=torch.float32, device=device)

        # [batch_size, max_n_blocks]
        src_sents_self_attn_bias = [[0.0] * len(inst) + [-1e18] * (self.max_para_num - len(inst))
                                    for inst in insts]
        src_sents_self_attn_bias = torch.tensor(src_sents_self_attn_bias, dtype=torch.float32, device=device)

        graphs = [[[1.0 - float(sim) for sim in list(row)] for row in g] for g in graphs]
        # [batch_size, max_n_blocks, max_n_blocks]
        graph_attn_bias = [self._pad(g, self.max_para_num, self.max_para_num, 1.0) for g in graphs]
        graph_attn_bias = torch.tensor(graph_attn_bias, dtype=torch.float32, device=device)

        return [src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias,
                src_sents_self_attn_bias, graph_attn_bias]

    def _pad_tgt_batch_data(self, insts, device):
        # [batch_size, max_tgt_len]
        tgt_words = [inst + [self.pad_idx] * (self.max_tgt_len - len(inst))
                     for inst in insts]
        tgt_words = torch.tensor(tgt_words, dtype=torch.int64, device=device)

        # [batch_size, max_tgt_len]
        tgt_pos = [list(range(0, len(inst))) + [0] * (self.max_tgt_len - len(inst))
                   for inst in insts]
        tgt_pos = torch.tensor(tgt_pos, dtype=torch.int64, device=device)

        # [batch_size, max_tgt_len, max_tgt_len]
        tgt_self_attn_bias = [[[1.0] * self.max_tgt_len] * self.max_tgt_len] * len(insts)
        # 上三角矩阵
        tgt_self_attn_bias = torch.triu(
            torch.tensor(tgt_self_attn_bias, dtype=torch.float32, device=device), diagonal=1
        ) * -1e18

        return [tgt_words, tgt_pos, tgt_self_attn_bias]

    def _pad_label_batch_data(self, insts, device):
        # [batch_size, max_tgt_len]
        tgt_label = [inst + [self.pad_idx] * (self.max_tgt_len - len(inst))
                     for inst in insts]
        tgt_label = torch.tensor(tgt_label, dtype=torch.int64, device=device)

        # [batch_size, max_tgt_len]
        label_weight = [[1.0] * len(inst) + [0.0] * (self.max_tgt_len - len(inst))
                        for inst in insts]
        label_weight = torch.tensor(label_weight, dtype=torch.float32, device=device)

        return [tgt_label, label_weight]


def load_dataset(args, phase, shuffle):
    assert phase in ['train', 'valid', 'test']

    def _lazy_dataset_loader(pt_file, phase):
        dataset = json.load(open(pt_file))
        logger.info('Loading %s dataset from %s, number of examples: %d' %
                    (phase, pt_file, len(dataset)))
        return dataset

    pts = sorted(glob.glob(args.data_path + '/' + phase + '/*.[0-9]*.json'))
    if pts:
        if shuffle:
            np.random.shuffle(pts)

        for pt in pts:
            yield _lazy_dataset_loader(pt, phase)
    else:
        pt = sorted(glob.glob(args.data_path + '/' + phase + '/*.json'))
        yield _lazy_dataset_loader(pt, phase)


class Dataloader(object):

    def __init__(self, args, datasets, symbols, batch_size, device,
                 shuffle, is_test, random_seed=None):
        self.args = args
        self.datasets = datasets
        self.symbols = symbols
        self.batch_size = batch_size
        self.device = device
        self.shuffle = shuffle
        self.is_test = is_test
        self.cur_iter = self._next_dataset_iterator(datasets)
        assert self.cur_iter is not None

        if not random_seed:
            random_seed = 0
        np.random.seed(random_seed)

    def __iter__(self):
        dataset_iter = (d for d in self.datasets)
        while self.cur_iter is not None:
            for batch in self.cur_iter:
                yield batch
            self.cur_iter = self._next_dataset_iterator(dataset_iter)

    def _next_dataset_iterator(self, dataset_iter):
        try:
            if hasattr(self, 'cur_dataset'):
                self.cur_dataset = None
                gc.collect()
                del self.cur_dataset
                gc.collect()

            self.cur_dataset = next(dataset_iter)
        except StopIteration:
            return None

        return DataIterator(args=self.args, dataset=self.cur_dataset, symbols=self.symbols,
                            batch_size=self.batch_size, device=self.device,
                            is_test=self.is_test, shuffle=self.shuffle)


class DataIterator(object):

    def __init__(self, args, dataset, symbols, batch_size, graph_type='similarity',
                 device=None, is_test=False, shuffle=True):
        self.args = args
        self.max_para_num = self.args.max_para_num
        self.max_para_len = self.args.max_para_len
        self.max_tgt_len = self.args.max_tgt_len

        self.dataset = dataset
        self.batch_size = batch_size
        self.graph_type = graph_type
        self.device = device
        self.is_test = is_test
        self.shuffle = shuffle

        self.symbols = symbols
        self.eos_idx = self.symbols['EOS']

        assert self.graph_type == 'similarity'

        self.iterations = 0

        self.secondary_sort_key = lambda x: sum([len(xi) for xi in x[0]])
        self.primary_sort_key = lambda x: len(x[1])
        self._iterations_this_epoch = 0

    def data(self):
        if self.shuffle:
            np.random.shuffle(self.dataset)
        xs = self.dataset
        return xs

    def preprocess(self, ex):
        src, tgt, tgt_str, graph = ex['src'], ex['tgt'], ex['tgt_str'], ex['sim_graph']

        src = src[:self.max_para_num]
        src = [para[:self.max_para_len] for para in src]

        graph = graph[:self.max_para_num]
        graph = [sim[:self.max_para_num] for sim in graph]

        tgt = tgt[:self.max_tgt_len][:-1] + [self.eos_idx]
        tgt_ids = tgt[:-1]
        label_ids = tgt[1:]

        return src, tgt_ids, label_ids, tgt_str, graph

    def simple_batch_size_fn(self, new, count):
        src, tgt = new[0], new[1]
        global max_src_in_batch, max_tgt_in_batch
        if count == 1:
            max_src_in_batch = 0
        max_src_in_batch = max(max_src_in_batch, len(src))
        src_elements = count * max_src_in_batch
        return src_elements

    def get_batch(self, data, batch_size):
        batch, max_len = [], 0
        for ex in data:
            max_len = max(max_len, len(ex[1]))
            if self.args.in_tokens:
                to_append = (len(batch) + 1) * max_len <= batch_size
            else:
                to_append = len(batch) < batch_size
            if to_append:
                batch.append(ex)
            else:
                yield batch
                batch, max_len = [ex], len([ex[1]])
        if batch:
            yield batch

    def batch_buffer(self, data, batch_size):
        batch, max_len = [], 0
        for ex in data:
            ex = self.preprocess(ex)
            max_len = max(max_len, len(ex[1]))
            if self.args.in_tokens:
                to_append = (len(batch) + 1) * max_len <= batch_size
            else:
                to_append = len(batch) < batch_size
            if to_append:
                batch.append(ex)
            else:
                yield batch
                batch, max_len = [ex], len(ex[1])

        if batch:
            yield batch

    def create_batches(self):
        data = self.data()
        for buffer in self.batch_buffer(data, self.batch_size * 100):
            if self.args.mode != 'train':
                p_batch = self.get_batch(
                    sorted(sorted(buffer, key=self.primary_sort_key), key=self.secondary_sort_key),
                    self.batch_size
                )
            else:
                p_batch = self.get_batch(
                    sorted(sorted(buffer, key=self.secondary_sort_key), key=self.primary_sort_key),
                    self.batch_size
                )
            # list 可以迭代完 get_batch
            p_batch = list(p_batch)

            if self.shuffle:
                np.random.shuffle(p_batch)
            for batch in p_batch:
                if len(batch) == 0:
                    continue
                yield batch

    def __iter__(self):
        while True:
            self.batches = self.create_batches()
            for idx, mini_batch in enumerate(self.batches):
                if self._iterations_this_epoch > idx:
                    continue
                self.iterations += 1
                self._iterations_this_epoch += 1
                batch = DataBatch(self.args, mini_batch, self.symbols['PAD'], self.device, self.is_test)

                yield batch
            return


class MultiNewsReader(object):

    def __init__(self, max_para_num=30, max_para_len=60, max_tgt_len=150,
                 graph_type="similarity", in_tokens=False, random_seed=None,
                 bos_idx=0, eos_idx=1, pad_idx=2, n_heads=8):
        self.max_para_num = max_para_num
        self.max_para_len = max_para_len
        self.max_tgt_len = max_tgt_len
        self.graph_type = graph_type
        self.in_tokens = in_tokens
        self.n_heads = n_heads

        self.bos_idx = bos_idx
        self.eos_idx = eos_idx
        self.pad_idx = pad_idx

        if not random_seed:
            random_seed = 0
        np.random.seed(random_seed)

        self.trainer_id = 0
        self.trainer_nums = 1

        self.current_example = 0
        self.current_epoch = 0
        self.num_examples = -1
        self.features = {}

    def load_dataset(self, data_path, shuffle=False):

        def _dataset_loader(pt_file):
            dataset = json.load(open(pt_file))
            logger.info("Loading dataset from %s, number of examples: %d" % (pt_file, len(dataset)))
            return dataset

        pts = sorted(glob.glob(data_path + "/*.[0-9]*.json"))
        if pts:
            if shuffle:
                np.random.shuffle(pts)

            datasets = []
            for pt in pts:
                datasets.extend(_dataset_loader(pt))
            return datasets
        else:
            pts = sorted(glob.glob(data_path + "/*.json"))
            datasets = _dataset_loader(pts[0])
            if shuffle:
                np.random.shuffle(datasets)

            return datasets

    def lazy_load_dataset(self, data_path, shuffle=False):

        def _dataset_loader(pt_file):
            dataset = json.load(open(pt_file))
            logger.info("Loading dataset from %s, number of examples: %d" % (pt_file, len(dataset)))
            return dataset

        pts = sorted(glob.glob(data_path + "/*.[0-9]*.json"))
        if pts:
            if shuffle:
                np.random.shuffle(pts)
            for pt in pts:
                yield _dataset_loader(pt)
        else:
            pts = sorted(glob.glob(data_path + "/*.json"))
            yield _dataset_loader(pts[0])

    def get_num_examples(self, data_path):
        if self.num_examples != -1:
            return self.num_examples

        num_examples = 0
        dataset_loader = self.lazy_load_dataset(data_path)
        for dataset in dataset_loader:
            num_examples += len(dataset)
        self.num_examples = num_examples

        return self.num_examples

    def get_train_progress(self):
        return self.current_example, self.current_epoch

    def _read_examples(self, data_path, shuffle=False):
        data_id = 0
        reader = self.load_dataset(data_path, shuffle)
        Example = namedtuple("Example", ["src", "tgt", "tgt_str", "graph", "data_id"])

        assert self.graph_type == "similarity"

        examples = []
        for ex in reader:
            graph = ex["sim_graph"]
            examples.append(Example(src=ex["src"], tgt=ex["tgt"], tgt_str=ex["tgt_str"],
                                    graph=graph, data_id=data_id))
            data_id += 1

        return examples

    def _example_reader(self, data_path, shuffle=False):
        data_id = 0
        reader = self.load_dataset(data_path, shuffle)
        Example = namedtuple("Example", ["src", "tgt", "tgt_str", "graph", "data_id"])

        for dataset in reader:
            if shuffle:
                np.random.shuffle(dataset)

            for ex in dataset:
                assert self.graph_type == "similarity"
                graph = ex['sim_graph']

                ex = Example(src=ex["src"], tgt=ex["tgt"], tgt_str=ex["tgt_str"],
                             graph=graph, data_id=data_id)
                data_id += 1

                yield ex

    def _convert_example_to_record(self, example):
        tgt = example.tgt[:self.max_tgt_len][:-1] + [self.eos_idx]
        # 截断过多段落
        src = [sent[:self.max_para_len] for sent in example.src]
        src = src[:self.max_para_num]

        graph = example.graph[:self.max_para_num]
        graph = [sim[:self.max_para_num] for sim in graph]

        Record = namedtuple("Record", ["src_ids", "tgt_ids", "label_ids", "graph", "data_id"])
        # 预测下一个词
        record = Record(src, tgt[:-1], tgt[1:], graph, example.data_id)
        return record

    def _prepare_batch_data(self, examples, batch_size, phase=None, do_dec=False, place=None):
        batch_records, max_len = [], 0
        index = 0
        for example in examples:
            if phase == "train":
                self.current_example = index
            record = self._convert_example_to_record(example)

            max_len = max(max_len, len(record.tgt_ids))
            # in_tokens 为 True 时，batch_size 为一个 batch 中最大的 token 数量
            # 否则，batch_size 为最大 example 的数量
            if self.in_tokens:
                to_append = (len(batch_records) + 1) * max_len <= batch_size
            else:
                to_append = len(batch_records) < batch_size
            if to_append:
                batch_records.append(record)
            else:
                yield self._pad_batch_records(batch_records, do_dec, place)
                batch_records, max_len = [record], len(record.tgt_ids)
            index += 1

        if batch_records:
            yield self._pad_batch_records(batch_records, do_dec, place)

    def get_features(self, phase):
        return self.features[phase]

    def data_generator(self, data_path, batch_size, epoch, dev_count=1,
                       shuffle=True, phase=None, do_dec=False, place=None):
        examples = self._read_examples(data_path)

        if phase != "train":
            features = {}
            for example in examples:
                features[example.data_id] = example
            self.features[phase] = features

        def wrapper():
            all_dev_batches = []
            for epoch_index in range(epoch):
                if phase == "train":
                    self.current_example = 0
                    self.current_epoch = epoch_index
                    trainer_id = self.trainer_id
                else:
                    trainer_id = 0
                    assert dev_count == 1

                if shuffle:
                    np.random.shuffle(examples)

                for batch_data in self._prepare_batch_data(
                        examples, batch_size, phase=phase, do_dec=do_dec, place=place
                ):
                    if len(all_dev_batches) < dev_count:
                        all_dev_batches.append(batch_data)
                    if len(all_dev_batches) == dev_count:
                        yield all_dev_batches[trainer_id]
                        all_dev_batches = []

        return wrapper

    def _pad_batch_records(self, batch_records, do_dec, place):
        if do_dec:
            return self._prepare_infer_input(batch_records, place=place)
        else:
            return self._prepare_train_input(batch_records)

    def _prepare_train_input(self, insts):
        src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias, \
        src_sents_self_attn_bias, graph_attn_bias = self._pad_src_batch_data(
            insts=[inst.src_ids for inst in insts],
            graphs=[inst.graph for inst in insts]
        )

        tgt_words, tgt_pos, tgt_self_attn_bias = self._pad_tgt_batch_data(
            insts=[inst.tgt_ids for inst in insts]
        )

        tgt_label, label_weight = self._pad_label_batch_data(
            insts=[inst.label_ids for inst in insts]
        )

        src_words = torch.tensor(src_words_pos, dtype=torch.int64)
        src_words_pos = torch.tensor(src_words_pos, dtype=torch.int64)
        src_sents_pos = torch.tensor(src_sents_pos, dtype=torch.int64)
        src_words_self_attn_bias = torch.tensor(src_words_self_attn_bias, dtype=torch.float32)
        src_sents_self_attn_bias = torch.tensor(src_sents_self_attn_bias, dtype=torch.float32)
        graph_attn_bias = torch.tensor(graph_attn_bias, dtype=torch.float32)
        tgt_words = torch.tensor(tgt_words, dtype=torch.int64)
        tgt_pos = torch.tensor(tgt_pos, dtype=torch.int64)
        tgt_self_attn_bias = torch.tensor(tgt_self_attn_bias, dtype=torch.float32)
        tgt_label = torch.tensor(tgt_label, dtype=torch.int64)
        label_weight = torch.tensor(label_weight, dtype=torch.float32)

        src_words_self_attn_bias = src_words_self_attn_bias.unsqueeze(2).unsqueeze(3) \
            .expand(-1, -1, self.n_heads, self.max_para_len, -1)
        src_words_self_attn_bias.requires_grad_(requires_grad=False)

        src_sents_self_attn_bias = src_sents_self_attn_bias.unsqueeze(1).unsqueeze(2) \
            .expand(-1, self.n_heads, self.max_para_num, -1)
        src_sents_self_attn_bias.requires_grad_(requires_grad=False)

        graph_attn_bias = graph_attn_bias.unsqueeze(1).expand(-1, self.n_heads, -1, -1)
        graph_attn_bias.requires_grad_(requires_grad=False)

        tgt_self_attn_bias = tgt_self_attn_bias.unsqueeze(1).expand(-1, self.n_heads, -1, -1)
        tgt_self_attn_bias.requires_grad_(requires_grad=False)

        tgt_src_words_attn_bias = src_words_self_attn_bias[:, :, :, 0].unsqueeze(3) \
            .expand(-1, -1, -1, self.max_tgt_len, -1)
        tgt_src_words_attn_bias.requires_grad_(requires_grad=False)

        tgt_src_sents_attn_bias = src_sents_self_attn_bias[:, :, 0].unsqueeze(2) \
            .expand(-1, -1, self.max_tgt_len, -1)
        tgt_src_sents_attn_bias.requires_grad_(requires_grad=False)

        src_words = src_words.view(-1, self.max_para_num, self.max_para_len, 1)
        src_words_pos = src_words_pos.view(-1, self.max_para_num, self.max_para_len, 1)
        src_sents_pos = src_sents_pos.view(-1, self.max_para_num, 1)
        tgt_words = tgt_words.view(-1, self.max_tgt_len, 1)
        tgt_pos = tgt_pos.view(-1, self.max_tgt_len, 1)
        tgt_label = tgt_label.view(-1, 1)
        label_weight = label_weight.view(-1, 1)

        enc_input = (src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias,
                     src_sents_self_attn_bias, graph_attn_bias)
        dec_input = (tgt_words, tgt_pos, tgt_self_attn_bias, tgt_src_words_attn_bias,
                     tgt_src_sents_attn_bias, graph_attn_bias)

        return {
            "enc_input": enc_input,
            "dec_input": dec_input,
            "tgt_label": tgt_label,
            "label_weight": label_weight
        }

    def _prepare_infer_input(self, insts, place):
        # TODO
        src_words, src_words_pos, src_sents_pos, src_words_self_attn_bias, \
        src_sents_self_attn_bias, graph_attn_bias = self._pad_src_batch_data(
            insts=[inst.src_ids for inst in insts],
            graphs=[inst.graph for inst in insts]
        )

        trg_word = np.array([[self.bos_idx]] * len(insts), dtype="int64")

    def _pad_matrix(self, data, height, width, pad_id):
        # padding 输入为 height 个 paragraphs，每个 paragraph 有 width 个单词
        rtn_data = [d + [pad_id] * (width - len(d)) for d in data]
        rtn_data = rtn_data + [[pad_id] * width] * (height - len(data))
        return rtn_data

    def _pad_src_batch_data(self, insts, graphs):
        return_list = []

        # [batch_size, max_n_blocks, max_n_tokens]
        inst_data = np.array([self._pad_matrix(inst, self.max_para_num, self.max_para_len, self.pad_idx)
                              for inst in insts], dtype="int64")
        return_list += [inst_data]

        # [batch_size, max_n_blocks, max_n_tokens]
        inst_words_pos = np.array([[list(range(0, len(para))) + [0] * (self.max_para_len - len(para))
                                    for para in inst] + [[0] * self.max_para_len] * (self.max_para_num - len(inst))
                                   for inst in insts], dtype="int64")
        return_list += [inst_words_pos]

        inst_sents_pos = np.array([list(range(0, len(inst))) +
                                   [0] * (self.max_para_num - len(inst)) for inst in insts])
        return_list += [inst_sents_pos]

        # 在 paddings 上不计算 attention
        # [batch_size, max_n_blocks, max_n_tokens]
        src_words_self_attn_bias_data = np.array([[[0.0] * len(para) + [-1e18] * (self.max_para_len - len(para))
                                                   for para in inst] +
                                                  [[-1e18] * self.max_para_len] * (self.max_para_num - len(inst))
                                                  for inst in insts], dtype="float32")
        return_list += [src_words_self_attn_bias_data]

        # [batch_size, max_n_blocks]
        src_sents_self_attn_bias_data = np.array([[0.0] * len(inst) + [-1e18] * (self.max_para_num - len(inst))
                                                  for inst in insts], dtype="float32")
        return_list += [src_sents_self_attn_bias_data]

        graphs = [[[1.0 - float(sim) for sim in list(row)] for row in g] for g in graphs]
        # [batch_size, max_n_blocks, max_n_blocks]
        graph_attn_bias = np.array([self._pad_matrix(g, self.max_para_num, self.max_para_num, 1.0)
                                    for g in graphs], dtype="float32")
        return_list += [graph_attn_bias]

        return return_list

    def _pad_tgt_batch_data(self, insts):
        return_list = []

        # [batch_size, max_tgt_len]
        inst_data = np.array([inst + [self.pad_idx] * (self.max_tgt_len - len(inst))
                              for inst in insts], dtype="int64")
        return_list += [inst_data]

        # [batch_size, max_tgt_len]
        inst_pos = np.array([list(range(0, len(inst))) + [0] * (self.max_tgt_len - len(inst))
                             for inst in insts], dtype="int64")
        return_list += [inst_pos]

        # [batch_size, max_tgt_len, max_tgt_len]
        self_attn_bias_data = np.ones((len(insts), self.max_tgt_len, self.max_tgt_len), dtype="float32")
        # 上三角矩阵
        self_attn_bias_data = np.triu(self_attn_bias_data, 1) * -1e18
        return_list += [self_attn_bias_data]

        return return_list

    def _pad_label_batch_data(self, insts):
        return_list = []

        # [batch_size, max_tgt_len]
        inst_data = np.array([inst + [self.pad_idx] * (self.max_tgt_len - len(inst))
                              for inst in insts], dtype="int64")
        return_list += [inst_data]

        # [batch_size, max_tgt_len]
        inst_weight = np.array([[1.] * len(inst) + [0.] * (self.max_tgt_len - len(inst))
                                for inst in insts], dtype="float32")
        return_list += [inst_weight]

        return return_list


if __name__ == '__main__':
    # data_reader = MultiNewsReader()
    # data_loader = data_reader.data_generator(
    #     data_path="/mnt/e/Projects/GraduationDesign/data/MultiNews/train",
    #     batch_size=16,
    #     epoch=10,
    #     phase="train"
    # )
    # for data in data_loader():
    #     print(data)
    #     break

    pt = "/mnt/d/Downloads/BrowserD/WIKI.train.31.pt"
    pt1 = "/mnt/e/Projects/GraduationDesign/data/MultiNews/train/MultiNews.30.train.11.json"
    dataset = json.load(open(pt1))
    pass