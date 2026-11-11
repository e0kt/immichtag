"""Checks against the real PixAI model. Skipped unless the weights are cached and a GPU exists."""

import numpy as np
import pytest
import torch

from immichtag import tagger

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def gpu_tagger():
    return tagger.load_model("auto")


def test_rope_buffers_stay_complex_and_weights_fp16(gpu_tagger):
    buffers = [b for name, b in gpu_tagger.model.named_buffers() if name.endswith("freqs_cis")]
    assert buffers, "model has no RoPE buffers"
    assert all(b.dtype == torch.complex64 for b in buffers)
    assert gpu_tagger.model.blocks[0].attn.qkv.weight.dtype == torch.float16


def test_preprocess_matches_upstream_processor(fixtures_dir):
    from PIL import Image
    from transformers import AutoImageProcessor

    processor = AutoImageProcessor.from_pretrained(
        tagger.MODEL_REPO, revision=tagger.MODEL_REVISION, trust_remote_code=True)
    for name in ("a.jpg", "b.png"):
        path = fixtures_dir / "images" / name
        ours = tagger.preprocess(path)
        theirs = processor(Image.open(path))["pixel_values"]
        assert ours.shape == theirs.shape == (1, 3, 1008, 1008)
        assert torch.max(torch.abs(ours - theirs)).item() < 1e-5


def test_fp16_matches_fp32_reference(gpu_tagger, fixtures_dir):
    reference = tagger.load_model("cpu")          # fp32
    for name in ("a.jpg", "b.png"):
        x = tagger.preprocess(fixtures_dir / "images" / name)
        fast = gpu_tagger.infer(x)
        slow = reference.infer(x)
        assert fast.shape == slow.shape == (len(gpu_tagger.names),)
        assert np.max(np.abs(fast - slow)) < 0.01
