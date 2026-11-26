"""Спільне для лабораторної 6: варіант, дані MedMNIST, мережа, оцінка якості.

Варіант 12: датасет Д6 (OCTMNIST, K = 4, C = 1), ширина Ш1 (16 / 32 / 128), P_max = 4, B = 384, K_c = 10, s_f = 25.
"""
import os
import urllib.request
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.utils.data import TensorDataset

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DATASETS = {               # позначка: (файл, класів K, каналів C)
    "Д1": ("dermamnist", 7, 3),
    "Д2": ("bloodmnist", 8, 3),
    "Д3": ("organamnist", 11, 1),
    "Д4": ("organcmnist", 11, 1),
    "Д5": ("pathmnist", 9, 3),
    "Д6": ("octmnist", 4, 1),
}
WIDTHS = {"Ш1": (16, 32, 128), "Ш2": (32, 64, 256), "Ш3": (64, 128, 512)}

# варіант 12
VARIANT = dict(dataset="Д6", width="Ш1", P_max=4, B=384, K_c=10, s_f=25)
N_TRAIN = 7000
LR0 = 0.02
MOMENTUM = 0.9
N_EP = 3
WARMUP_STEPS = 10
MEASURE_STEPS = 50

NAME, K, C = DATASETS[VARIANT["dataset"]]
C1, C2, H = WIDTHS[VARIANT["width"]]
ZENODO = "https://zenodo.org/records/10519652/files"


def npz_path():
    return DATA / f"{NAME}.npz"


def ensure_data():
    """Завантажує data/octmnist.npz з Zenodo, якщо файлу ще немає."""
    p = npz_path()
    if p.exists():
        return p
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    print(f"завантажую {NAME}.npz з Zenodo ...", flush=True)
    urllib.request.urlretrieve(f"{ZENODO}/{NAME}.npz?download=1", tmp)
    os.replace(tmp, p)
    return p


def load_split(path, split, n=None):
    """Розбиття split ("train" або "test") файлу .npz як TensorDataset."""
    d = np.load(path)
    x = d[f"{split}_images"][:n]   # (N, 28, 28) або (N, 28, 28, 3), uint8
    y = d[f"{split}_labels"][:n].reshape(-1)  # (N, 1) -> (N,)
    if x.ndim == 3:
        x = x[:, None, :, :]                  # (N, 1, 28, 28)
    else:
        x = x.transpose(0, 3, 1, 2)           # (N, 3, 28, 28)
    x = torch.from_numpy(np.ascontiguousarray(x)).float()
    x = (x / 255 - 0.5) / 0.5
    return TensorDataset(x, torch.from_numpy(y.astype(np.int64)))


class Net(nn.Module):
    def __init__(self, C, c1, c2, h, K):
        super().__init__()
        self.features = nn.Sequential(
            # 28 x 28 -> 14 x 14
            nn.Conv2d(C, c1, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            # 14 x 14 -> 7 x 7
            nn.Conv2d(c1, c2, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        )
        self.head = nn.Sequential(
            nn.Flatten(), nn.Linear(7 * 7 * c2, h), nn.ReLU(),
            nn.Linear(h, K),
        )

    def forward(self, x):
        return self.head(self.features(x))


def n_params_formula(C, c1, c2, h, K):
    return 9 * C * c1 + c1 + 9 * c1 * c2 + c2 + 49 * c2 * h + h + h * K + K


def make_model():
    torch.manual_seed(0)
    model = Net(C, C1, C2, H, K)
    n = sum(p.numel() for p in model.parameters())
    assert n == n_params_formula(C, C1, C2, H, K)
    return model


@torch.no_grad()
def evaluate(model, test):
    """ACC і macro AUC (one-vs-rest) на тестовій частині."""
    from sklearn.metrics import roc_auc_score
    model.eval()
    x, y = test.tensors
    probs = []
    for i in range(0, len(x), 1000):
        probs.append(torch.softmax(model(x[i:i + 1000]), dim=1))
    prob = torch.cat(probs).numpy()
    model.train()
    acc = float((prob.argmax(1) == y.numpy()).mean())
    auc = float(roc_auc_score(y.numpy(), prob, multi_class="ovr", average="macro"))
    return acc, auc
