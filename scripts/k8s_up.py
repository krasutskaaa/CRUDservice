#!/usr/bin/env python3
"""Команда розгортання лабораторної 3 (k3d, варіант 12).

  python scripts/k8s_up.py               # кластер + образ + усі маніфести + моніторинг
  python scripts/k8s_up.py --no-cluster  # кластер уже є: лише образ і маніфести
  python scripts/k8s_up.py --no-build    # не перезбирати образ

Кроки: k3d cluster create → kubectl label node role=infra → docker build → k3d image import →
Secret з .env → ConfigMap зі схемою і запитами експортера → база (StatefulSet) → Job міграції →
web → моніторинг (Prometheus, kube-state-metrics, postgres-exporter). metrics-server входить до k3s.
"""
import argparse
import subprocess
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT  # noqa: E402
from k8s_common import CLUSTER, IMAGE, NS, SERVER_NODE, apply_stdin, kubectl  # noqa: E402


def read_env(path):
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def run(*cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def cluster_exists():
    r = subprocess.run(["k3d", "cluster", "list", "--no-headers"], capture_output=True, text=True)
    return any(line.split()[0] == CLUSTER for line in r.stdout.splitlines() if line.strip())


def configmap_from_file(name, key, path):
    yaml = kubectl("create", "configmap", name, f"--from-file={key}={path}",
                   "--dry-run=client", "-o", "yaml", capture=True).stdout
    apply_stdin(yaml)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cluster", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    a = ap.parse_args()

    env_file = ROOT / ".env"
    if not env_file.exists():
        sys.exit("немає файла .env (скопіюйте .env.example і задайте POSTGRES_PASSWORD)")
    password = read_env(env_file).get("POSTGRES_PASSWORD")
    if not password:
        sys.exit("у .env не задано POSTGRES_PASSWORD")

    if not a.no_cluster and not cluster_exists():
        run("k3d", "cluster", "create", "--config", str(ROOT / "k3d" / "cluster.yaml"))
    kubectl("label", "node", SERVER_NODE, "role=infra", "--overwrite", ns=False)

    if not a.no_build:
        run("docker", "build", "-t", IMAGE, ".")
    run("k3d", "image", "import", IMAGE, "-c", CLUSTER)

    kubectl("apply", "-f", ROOT / "k8s" / "namespace.yaml", ns=False)
    # пароль лише в Secret кластера: ні в образі, ні в ConfigMap, ні в репозиторії
    secret = kubectl("create", "secret", "generic", "db-secret",
                     f"--from-literal=POSTGRES_PASSWORD={password}",
                     "--dry-run=client", "-o", "yaml", capture=True).stdout
    apply_stdin(secret)
    configmap_from_file("db-schema", "init.sql", ROOT / "db" / "init.sql")
    configmap_from_file("pg-exporter-queries", "queries.yaml", ROOT / "postgres-exporter" / "queries.yaml")

    kubectl("apply", "-f", ROOT / "k8s" / "db.yaml")
    kubectl("rollout", "status", "statefulset/db", "--timeout=300s")
    kubectl("delete", "job", "migrate", "--ignore-not-found")  # Job незмінний: перезапуск = створити заново
    kubectl("apply", "-f", ROOT / "k8s" / "migrate-job.yaml")
    kubectl("wait", "--for=condition=complete", "job/migrate", "--timeout=300s")

    kubectl("apply", "-f", ROOT / "k8s" / "web.yaml")
    if not a.no_build:
        kubectl("rollout", "restart", "deployment/web")  # той самий тег, новий образ
    kubectl("rollout", "status", "deployment/web", "--timeout=300s")

    kubectl("apply", "-f", ROOT / "k8s" / "monitoring")
    for d in ("prometheus", "kube-state-metrics", "postgres-exporter"):
        kubectl("rollout", "status", f"deployment/{d}", "--timeout=300s")
    kubectl("get", "pods", "-o", "wide")
    print(f"готово: web http://127.0.0.1:8080, Prometheus http://127.0.0.1:9090 (простір імен {NS})")


if __name__ == "__main__":
    t = time.time()
    main()
    print(f"розгортання: {time.time() - t:.0f} с")
