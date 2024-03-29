# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import argparse
from typing import Optional

import lm_eval

import torch
from lm_eval.api.model import LM
from lm_eval.evaluator import evaluate
from lm_eval.models.huggingface import HFLM as eval_wrapper
from lm_eval.tasks import get_task_dict
from sentencepiece import SentencePieceProcessor
from torch import nn

from .builder import LlamaEdgeManager
from .export_llama_lib import (
    _prepare_for_llama_export,
    build_args_parser as _build_args_parser,
)

def setup_cache_padded_seq_input_pos_max_seq_length_for_prefill(
    model: torch.nn.Module,
    prompt: torch.Tensor,
    max_new_tokens: int,
    max_seq_length: Optional[int] = None,
    block_size: int = 2048,
):
    """
    Sets up model cache and does some bookkeeping calculations for prompt, input_pos and max_seq_length
    that are needed for prefill or model_forward

    Args:
        model (torch.nn.Module): The model whose cache gets set up
        prompt (torch.Tensor): Tensor of shape (T) with indices of the prompt sequence.
        max_new_tokens (int): The desired maximum number of new tokens that can be generated.
        max_seq_length (Optional[int], optional): The maximum sequence length allowed.

    Returns:
        seq (torch.Tensor): prompt but padded with zeros to size max_seq_length
        input_pos (torch.Tensor): tensor of integers in increasing order
        max_seq_length (int): The maximum sequence length allowed, updated based on other numbers
    """
    T = prompt.size(0)
    T_new = T + max_new_tokens
    if max_seq_length is None:
        max_seq_length = min(T_new, block_size)

    device, dtype = prompt.device, prompt.dtype
    # create an empty tensor of the expected final shape and fill in the current tokens
    empty = torch.empty(T_new, dtype=dtype, device=device)
    empty[:T] = prompt
    seq = empty
    input_pos = torch.arange(0, T, device=device)

    # no caches in executorch llama2 7b model?
    # with torch.device(device):
    #     model.setup_caches(max_batch_size=1, max_seq_length=max_seq_length)

    return seq, input_pos, max_seq_length


class GPTFastEvalWrapper(eval_wrapper):
    """
    A wrapper class based on GPTFast, providing integration with the lm-evaluation-harness library.
    """

    def __init__(
        self,
        model: nn.Module,
        tokenizer,
        max_seq_length: Optional[int] = None,
    ):
        super().__init__()
        self._model = model
        self._tokenizer = tokenizer
        self._device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        self._max_seq_length = 2048 if max_seq_length is None else max_seq_length

    @property
    def eot_token_id(self):
        return self._tokenizer.eos_id()

    @property
    def max_length(self):
        return self._max_seq_length

    @property
    def max_gen_toks(self):
        return 50

    @property
    def batch_size(self):
        return 1

    @property
    def device(self):
        return self._device

    def tok_encode(self, string: str, **kwargs):
        tokens = [self._tokenizer.bos_id()] + self._tokenizer.encode(string)
        encoded = torch.tensor(tokens, dtype=torch.int, device=self.device)
        # encoded is a pytorch tensor, but some internal logic in the
        # eval harness expects it to be a list instead
        # TODO: verify this for multi-batch as well
        encoded = encoded.tolist()
        return encoded

    def tok_decode(self, tokens):
        decoded = self._tokenizer.decode(tokens)
        return decoded

    # def _model_call(self, inps):
    #     # TODO: make batches work
    #     inps = inps.squeeze(0)

    #     max_new_tokens = 1
    #     seq, input_pos, max_seq_length = (
    #         setup_cache_padded_seq_input_pos_max_seq_length_for_prefill(
    #             self._model,
    #             inps,
    #             max_new_tokens,
    #             self.max_length,
    #         )
    #     )
    #     x = seq.index_select(0, input_pos).view(1, -1)
    #     logits = self._model(x, input_pos)
    #     return logits

    def _model_call(self, inps):
        return self._model(inps)

    def _model_generate(self, context, max_length, eos_token_id):
        raise Exception("unimplemented")


@torch.no_grad()
def eval(
    eval_wrapper: LM,
    tasks: Optional[list] = None,
    limit: Optional[int] = None,
) -> dict:
    """
    Evaluates a language model on a specified task using the lm-evaluation-harness library.

    Args:
        eval_wrapper (LM): A LM wrapper class compatible with lm-evaluation-harness evaluation
        task (str): The name of the evaluation task to perform.
        limit (Optional[int]): The maximum number of samples to evaluate (None for all available).

    Returns:p
        eval_results (dict): A dictionary of evaluation results for the specified task(s).
    """

    if tasks is None:
        tasks = ["wikitext"]

    if "hendrycks_test" in tasks:
        tasks.remove("hendrycks_test")
        tasks += list(lm_eval.tasks.hendrycks_test.create_all_tasks().keys())
    task_dict = get_task_dict(tasks)

    eval_results = evaluate(
        eval_wrapper,
        task_dict,
        limit=limit,
    )
    return eval_results


def gen_eval_wrapper(
    model_name: str,
    args: argparse.ArgumentParser,
) -> LM:
    """
    Generates a wrapper interface around the provided model and tokenizer for
    the lm-evaluation-harness library.

    Returns:
        eval_wrapper (LM): A wrapper interface for the lm-evaluation-harness library.
    """
    tokenizer = SentencePieceProcessor(model_file=str(args.tokenizer_path))

    # GPTFastEvalWrapper: Create a wrapper around a pre-exported model
    manager: LlamaEdgeManager = _prepare_for_llama_export(model_name, args)
    model = (
        manager.model.eval().to(device="cuda")
        if torch.cuda.is_available()
        else manager.model.to(device="cpu")
    )
    return GPTFastEvalWrapper(
        model=model,
        tokenizer=tokenizer,
        max_seq_length=args.max_seq_length,
    )


def build_args_parser() -> argparse.ArgumentParser:
    # Start with arg parser from export_llama_lib
    parser = _build_args_parser()

    # Add additional args specific to eval
    parser.add_argument(
        "--tasks",
        nargs="+",
        type=str,
        default=["wikitext"],
        help="list of lm-eluther tasks to evaluate usage: --tasks task1 task2",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="number of samples to evalulate"
    )

    return parser


def eval_llama(
    model_name: str,
    args: argparse.ArgumentParser,
) -> None:
    # Generate the eval wrapper
    eval_wrapper = gen_eval_wrapper(model_name, args)

    # Evaluate the model
    eval_results = eval(
        eval_wrapper,
        args.tasks,
        args.limit,
    )

    for task, res in eval_results["results"].items():
        print(f"{task}: {res}")
