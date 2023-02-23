# coding:utf-8

import math
import torch
import numpy as np
from torch import nn
from typing import Callable, Dict, Iterable, List, Tuple, Union, Any
from torch.optim import Optimizer
from numpy.lib.twodim_base import mask_indices
from torch.nn.utils.rnn import pad_sequence
from torch.optim.lr_scheduler import LambdaLR
from transformers import (
    WEIGHTS_NAME,
    AdamW,
    AutoConfig,
    AutoModelForMaskedLM,
    AutoTokenizer,
    PreTrainedModel,
    PreTrainedTokenizer,
    get_linear_schedule_with_warmup,
)

def freeze_params(model: nn.Module):
    """Set requires_grad=False for each of model.parameters()"""
    for par in model.parameters():
        par.requires_grad = False

def freeze_params_amr(model: nn.Module):
    """Set requires_grad=False for each of model.parameters()"""
    for name, par in model.named_parameters():
        if 'adapter' not in name:
            par.requires_grad = False

def freeze_embeds(model):
    """Freeze token embeddings and positional embeddings for bart, just token embeddings for t5."""
    model_type = model.config.model_type

    if model_type == "t5":
        freeze_params(model.shared)
        for d in [model.encoder, model.decoder]:
            freeze_params(d.embed_tokens)
    elif model_type == "fsmt":
        for d in [model.model.encoder, model.model.decoder]:
            freeze_params(d.embed_positions)
            freeze_params(d.embed_tokens)
    else:
        freeze_params(model.model.shared)
        for d in [model.model.encoder, model.model.decoder]:
            freeze_params(d.embed_positions)
            freeze_params(d.embed_tokens)


def act_params(model: nn.Module):
    """Set requires_grad=False for each of model.parameters()"""
    for par in model.parameters():
        par.requires_grad = True


def activate_embeds(model):
    """Freeze token embeddings and positional embeddings for bart, just token embeddings for t5."""
    model_type = model.config.model_type

    if model_type == "t5":
        act_params(model.shared)
        for d in [model.encoder, model.decoder]:
            act_params(d.embed_tokens)
    elif model_type == "fsmt":
        for d in [model.model.encoder, model.model.decoder]:
            act_params(d.embed_positions)
            act_params(d.embed_tokens)
    else:
        act_params(model.model.shared)
        for d in [model.model.encoder, model.model.decoder]:
            act_params(d.embed_positions)
            act_params(d.embed_tokens)


def assert_all_frozen(model):
    model_grads: List[bool] = list(grad_status(model))
    n_require_grad = sum(lmap(int, model_grads))
    npars = len(model_grads)
    assert not any(model_grads), f"{n_require_grad/npars:.1%} of {npars} weights require grad"


def grad_status(model: nn.Module) -> Iterable:
    return (par.requires_grad for par in model.parameters())


def lmap(f: Callable, x: Iterable) -> List:
    """list(map(f, x))"""
    return list(map(f, x))


def get_inverse_sqrt_schedule_with_warmup(
    optimizer: Optimizer, num_warmup_steps: int, num_training_steps: int, last_epoch: int = -1
):
    """
    Create a schedule with a learning rate that decreases following the values of the cosine function between the
    initial lr set in the optimizer to 0, after a warmup period during which it increases linearly between 0 and the
    initial lr set in the optimizer.

    Args:
        optimizer (:class:`~torch.optim.Optimizer`):
            The optimizer for which to schedule the learning rate.
        num_warmup_steps (:obj:`int`):
            The number of steps for the warmup phase.
        num_training_steps (:obj:`int`):
            The total number of training steps.
        num_cycles (:obj:`float`, `optional`, defaults to 0.5):
            The number of waves in the cosine schedule (the defaults is to just decrease from the max value to 0
            following a half-cosine).
        last_epoch (:obj:`int`, `optional`, defaults to -1):
            The index of the last epoch when resuming training.

    Return:
        :obj:`torch.optim.lr_scheduler.LambdaLR` with the appropriate schedule.
    """

    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        return max(0.0, (num_warmup_steps / current_step)**0.5)

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def sentence_infilling(tokenizer, inp, mlm_prob=0.35):
    token_length = len([int(itm != tokenizer.pad_token_id) for itm in inp])
    masking_length = math.floor(token_length * mlm_prob)
    masked_length = 1
    masked_inputs = inp.clone().tolist()
    while masked_length < masking_length:
        span_length = min(math.floor(np.random.poisson(3, 1)), token_length - 1)
        start_index = math.floor(np.random.uniform(1, token_length - span_length, 1))
        masked_inputs = masked_inputs[:start_index] + [tokenizer.mask_token_id] + masked_inputs[start_index + span_length:]
        token_length -= span_length - 1
        masked_length += span_length
    return torch.LongTensor(masked_inputs)


def text_infilling(inp, tokenizer, mlm_prob=0.35):
    res = []
    for sents in inp:
        res.append(sentence_infilling(tokenizer, sents, mlm_prob=mlm_prob))
    return pad_sequence(res, batch_first=True, padding_value=tokenizer.pad_token_id)


def get_text_infilling_inputs(batch, tokenizer, args, inp='text'):
    if inp == "text":
        ori_input = batch["input_ids"]
        masked_input = text_infilling(ori_input, tokenizer)
        attention_mask = masked_input.ne(tokenizer.pad_token_id).int()
        labels = ori_input.clone()
        labels.masked_fill_(labels == tokenizer.pad_token_id, -100)
        labels = labels[:, 1:]                      # [w1 w2 w3 ...]
        dec_input = ori_input[:, :-1]
        return masked_input, attention_mask, dec_input, labels
    
    elif inp == "amr":
        labels = batch["labels"]                                # [bsz, len+1]
        shifted_input_ids = labels.new_zeros(labels.size(0), labels.size(1) + 1)
        shifted_input_ids[:, 1:] = labels.clone()
        # shifted_input_ids[:, 0] = tokenizer.bos_token_id                    # <s> w1, w2, ..., wn <\s>
        shifted_input_ids[:, 0] = tokenizer.amr_bos_token_id                # <AMR> w1, w2, ..., wn <\s>
        shifted_input_ids.masked_fill_(shifted_input_ids == -100, tokenizer.pad_token_id)   # replace -100 with pad_token_id
        masked_input = text_infilling(shifted_input_ids, tokenizer)
        attention_mask = masked_input.ne(tokenizer.pad_token_id).int()     # attention mask
        dec_input = batch["decoder_input_ids"]
        return masked_input, attention_mask, dec_input, labels


def joint_infilling_partial(inp, seg_id, tokenizer, mask_txt=True, mlm_prob=0.35):
    res = []
    for inp_ids, seg_iid in zip(inp, seg_id):
        inp_ids = torch.LongTensor([iid for iid in inp_ids if iid != tokenizer.pad_token_id])
        text_ids = torch.LongTensor([inp_ids[idx] for idx in range(len(inp_ids)) if seg_iid[idx] == 0])
        amr_ids = torch.LongTensor([inp_ids[idx] for idx in range(len(inp_ids)) if seg_iid[idx] == 1])
        if mask_txt:
            res.append(torch.cat([sentence_infilling(tokenizer, text_ids, mlm_prob=mlm_prob), amr_ids], dim=0))
        else:
            res.append(torch.cat([text_ids, sentence_infilling(tokenizer, amr_ids, mlm_prob=mlm_prob)], dim=0))

    return pad_sequence(res, batch_first=True, padding_value=tokenizer.pad_token_id)


def get_full_textinf_joint_inputs_partial_output_dapt(batch, tokenizer, mlm_prob=0.35):
    ori_input = batch["input_ids"]
    seg_ids = batch["seg_ids"]
    masked_input = joint_infilling_partial(ori_input, seg_ids, tokenizer, mask_txt=True, mlm_prob=mlm_prob)
    attention_mask = masked_input.ne(tokenizer.pad_token_id).int()              # attention mask
    labels = batch["labels"]
    dec_input = labels.new_zeros(labels.size(0), labels.size(1))
    dec_input[:, 1:] = labels[:, :-1].clone()
    dec_input[:, 0] = tokenizer.bos_token_id                                # <s> w1 w2, ..., wn
    dec_input.masked_fill_(dec_input == -100, tokenizer.pad_token_id)
    return masked_input, attention_mask, dec_input, labels