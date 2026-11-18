"""crosslang.shrink: the model stored in 16 bits gives the same outputs, is accepted as complete, works when the
volume has no room (written via the temporary folder), and is skipped with CROSSLANG_FP16=0. A tiny ONNX model with
the same shape of graph as LaBSE (an embedding Gather and a MatMul) stands in; one test uses the real model when
CROSSLANG_TEST_MODEL points to its files."""
import json
import os
import shutil
from collections import namedtuple

import numpy as np
import onnx
import onnxruntime
import pytest
from onnx import TensorProto, helper, numpy_helper

import crosslang


def tiny_model(path):
    rng = np.random.default_rng(1)
    emb = rng.normal(size=(5000, 64)).astype(np.float32)          # >= 100,000 values: stored in 16 bits
    w = rng.normal(size=(64, 2000)).astype(np.float32)
    bias = rng.normal(size=(8,)).astype(np.float32)                # small: left as it is
    nodes = [helper.make_node("Gather", ["emb", "ids"], ["x"]),
             helper.make_node("MatMul", ["x", "w"], ["y"]),
             helper.make_node("ReduceMean", ["y"], ["out"], axes=[2], keepdims=0)]
    graph = helper.make_graph(nodes, "tiny", [helper.make_tensor_value_info("ids", TensorProto.INT64, [None, None])],
                              [helper.make_tensor_value_info("out", TensorProto.FLOAT, None)],
                              [numpy_helper.from_array(a, n) for a, n in ((emb, "emb"), (w, "w"), (bias, "unused"))])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    return path.stat().st_size


def run(path, ids):
    return onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"]).run(None, {"ids": ids})[0]


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    size = tiny_model(tmp_path / "labse" / crosslang.MODEL)
    for name in ("tokenizer.json", "2_Dense/model.safetensors"):
        (tmp_path / "labse" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "labse" / name).write_bytes(b"x" * 10)
    monkeypatch.setattr(crosslang, "FILES", {crosslang.MODEL: (size, None), "tokenizer.json": (10, None),
                                             "2_Dense/model.safetensors": (10, None)})
    monkeypatch.delenv("CROSSLANG_FP16", raising=False)
    return tmp_path / "labse"


IDS = np.array([[1, 7, 4999, 0], [12, 12, 300, 2]], dtype=np.int64)


def test_shrink_halves_the_file_and_keeps_the_outputs(folder):
    before = run(folder / crosslang.MODEL, IDS)
    full = (folder / crosslang.MODEL).stat().st_size
    assert crosslang.needs_shrink(folder) and crosslang.missing_files(folder) == []
    new = crosslang.shrink(folder)
    assert new < 0.55 * full and (folder / crosslang.MODEL).stat().st_size == new
    assert json.loads((folder / crosslang.SHRUNK).read_text())["size"] == new
    assert crosslang.missing_files(folder) == [] and not crosslang.needs_shrink(folder)   # accepted, not redone
    after = run(folder / crosslang.MODEL, IDS)
    assert np.allclose(before, after, rtol=1e-2, atol=1e-2)
    assert not list(folder.glob("onnx/*.part"))


def test_full_volume_goes_through_the_temporary_folder(folder, tmp_path, monkeypatch):
    before = run(folder / crosslang.MODEL, IDS)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10**9, 10**9, 0))   # no room beside the model
    spare = tmp_path / "spare"
    spare.mkdir()
    crosslang.shrink(folder, tmp_dir=spare)
    assert np.allclose(before, run(folder / crosslang.MODEL, IDS), rtol=1e-2, atol=1e-2)
    assert not list(spare.iterdir()) and crosslang.missing_files(folder) == []


def test_switched_off_and_a_damaged_marker(folder, monkeypatch):
    monkeypatch.setenv("CROSSLANG_FP16", "0")
    assert not crosslang.needs_shrink(folder)
    monkeypatch.delenv("CROSSLANG_FP16")
    crosslang.shrink(folder)
    (folder / crosslang.SHRUNK).write_text("not json")
    assert crosslang.missing_files(folder) == [crosslang.MODEL]          # then the full model is downloaded again


@pytest.mark.skipif(not os.environ.get("CROSSLANG_TEST_MODEL"), reason="real LaBSE files not available (set CROSSLANG_TEST_MODEL)")
def test_real_model_same_vectors_after_shrinking(tmp_path):
    source = os.environ["CROSSLANG_TEST_MODEL"]
    copy = tmp_path / "labse"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("*.json.bak"))
    (copy / crosslang.SHRUNK).unlink(missing_ok=True)
    sentences = ["Pul o‘tkazmalari qishloq oilalarining byudjetini sezilarli darajada o‘zgartirdi.",
                 "Денежные переводы существенно изменили бюджеты сельских семей.",
                 "Remittances significantly changed the budgets of rural families.",
                 "Boshlang‘ich sinf o‘quvchilarining nutqini o‘stirish metodikasi."]
    before = crosslang.Encoder(copy).encode(sentences)
    crosslang.shrink(copy)
    after = crosslang.Encoder(copy).encode(sentences)
    assert (copy / crosslang.MODEL).stat().st_size < 1.0e9
    assert np.min((before * after).sum(1)) > 0.9999
    assert np.max(np.abs(before @ before.T - after @ after.T)) < 0.001
