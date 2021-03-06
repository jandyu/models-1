# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import sys
import os
import io
import itertools
from functools import partial

import numpy as np
from paddle.io import BatchSampler, DataLoader, Dataset
from paddlenlp.data import Pad
from paddlenlp.datasets import WMT14ende
from paddlenlp.data.sampler import SamplerHelper


def min_max_filer(data, max_len, min_len=0):
    # 1 for special tokens.
    data_min_len = min(len(data[0]), len(data[1])) + 1
    data_max_len = max(len(data[0]), len(data[1])) + 1
    return (data_min_len >= min_len) and (data_max_len <= max_len)


def create_data_loader(args):
    root = None if args.root == "None" else args.root
    (src_vocab, trg_vocab) = WMT14ende.get_vocab(root=root)
    args.src_vocab_size, args.trg_vocab_size = len(src_vocab), len(trg_vocab)
    transform_func = WMT14ende.get_default_transform_func(root=root)
    datasets = [
        WMT14ende.get_datasets(
            mode=m, transform_func=transform_func) for m in ["train", "dev"]
    ]

    if args.shuffle or args.shuffle_batch:
        if args.shuffle_seed == "None" or args.shuffle_seed is None:
            shuffle_seed = 0
        else:
            shuffle_seed = args.shuffle_seed

    def _max_token_fn(current_idx, current_batch_size, tokens_sofar,
                      data_source):
        return max(tokens_sofar,
                   len(data_source[current_idx][0]) + 1,
                   len(data_source[current_idx][1]) + 1)

    def _key(size_so_far, minibatch_len):
        return size_so_far * minibatch_len

    data_loaders = [(None)] * 2
    for i, dataset in enumerate(datasets):
        m = dataset.mode
        dataset = dataset.filter(
            partial(
                min_max_filer, max_len=args.max_length))
        sampler = SamplerHelper(dataset)

        src_key = (lambda x, data_source: len(data_source[x][0]) + 1)
        if args.sort_type == SortType.GLOBAL:
            buffer_size = -1
            trg_key = (lambda x, data_source: len(data_source[x][1]) + 1)
            # Sort twice
            sampler = sampler.sort(
                key=trg_key, buffer_size=buffer_size).sort(
                    key=src_key, buffer_size=buffer_size)
        else:
            if args.shuffle:
                sampler = sampler.shuffle(seed=shuffle_seed)
            if args.sort_type == SortType.POOL:
                buffer_size = args.pool_size
                sampler = sampler.sort(key=src_key, buffer_size=buffer_size)

        batch_sampler = sampler.batch(
            batch_size=args.batch_size,
            drop_last=False,
            batch_size_fn=_max_token_fn,
            key=_key)

        if m == "train":
            batch_sampler = batch_sampler.shard()

        if args.shuffle_batch:
            batch_sampler.shuffle(seed=shuffle_seed)

        data_loader = DataLoader(
            dataset=dataset,
            batch_sampler=batch_sampler,
            collate_fn=partial(
                prepare_train_input,
                bos_idx=args.bos_idx,
                eos_idx=args.eos_idx,
                pad_idx=args.bos_idx),
            num_workers=0,
            return_list=True)
        data_loaders[i] = (data_loader)
    return data_loaders


def create_infer_loader(args):
    root = None if args.root == "None" else args.root
    (src_vocab, trg_vocab) = WMT14ende.get_vocab(root=root)
    args.src_vocab_size, args.trg_vocab_size = len(src_vocab), len(trg_vocab)
    transform_func = WMT14ende.get_default_transform_func(root=root)
    dataset = WMT14ende.get_datasets(
        mode="test", transform_func=transform_func).filter(
            partial(
                min_max_filer, max_len=args.max_length))

    batch_sampler = SamplerHelper(dataset).batch(
        batch_size=args.infer_batch_size, drop_last=False)

    data_loader = DataLoader(
        dataset=dataset,
        batch_sampler=batch_sampler,
        collate_fn=partial(
            prepare_infer_input,
            bos_idx=args.bos_idx,
            eos_idx=args.eos_idx,
            pad_idx=args.bos_idx),
        num_workers=0,
        return_list=True)
    return data_loader, trg_vocab.to_tokens


def prepare_train_input(insts, bos_idx, eos_idx, pad_idx):
    """
    Put all padded data needed by training into a list.
    """
    word_pad = Pad(pad_idx)
    src_word = word_pad([inst[0] + [eos_idx] for inst in insts])
    trg_word = word_pad([[bos_idx] + inst[1] for inst in insts])
    lbl_word = np.expand_dims(
        word_pad([inst[1] + [eos_idx] for inst in insts]), axis=2)

    data_inputs = [src_word, trg_word, lbl_word]

    return data_inputs


def prepare_infer_input(insts, bos_idx, eos_idx, pad_idx):
    """
    Put all padded data needed by beam search decoder into a list.
    """
    word_pad = Pad(pad_idx)
    src_word = word_pad([inst[0] + [eos_idx] for inst in insts])

    return [src_word, ]


class SortType(object):
    GLOBAL = 'global'
    POOL = 'pool'
    NONE = "none"
