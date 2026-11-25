"""Спільне для скриптів лабораторної 3: виклики kubectl і k3d (лише стандартна бібліотека)."""
import json
import os
import subprocess
import time

from common import ROOT, RESULTS

CLUSTER = "lab3"
NS = "lab3"
SERVER_NODE = f"k3d-{CLUSTER}-server-0"
IMAGE = "admissions-web:lab3"
# контекст, який k3d додає в kubeconfig; порожнє значення = поточний контекст
CONTEXT = os.environ.get("KUBE_CONTEXT", f"k3d-{CLUSTER}")
OUT = RESULTS / "lab3"


def kubectl(*args, check=True, capture=False, input=None, ns=True):
    cmd = ["kubectl"]
    if CONTEXT:
        cmd += ["--context", CONTEXT]
    if ns:
        cmd += ["-n", NS]
    cmd += [str(a) for a in args]
    r = subprocess.run(cmd, cwd=ROOT, check=False, input=input, text=True, encoding="utf-8",
                       errors="replace", capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} → код {r.returncode}\n{r.stderr if capture else ''}")
    return r


def kjson(*args, ns=True):
    return json.loads(kubectl(*args, "-o", "json", capture=True, ns=ns).stdout)


def apply_stdin(manifest_text):
    kubectl("apply", "-f", "-", input=manifest_text, ns=False)


def ready_web():
    """Кількість готових подів web (Ready=True і без deletionTimestamp)."""
    pods = kjson("get", "pods", "-l", "app=web")["items"]
    n = 0
    for p in pods:
        if p["metadata"].get("deletionTimestamp"):
            continue
        conds = {c["type"]: c["status"] for c in p.get("status", {}).get("conditions", [])}
        n += conds.get("Ready") == "True"
    return n, len(pods)


def wait_web(n, timeout=240):
    """Чекає рівно n готових подів web і жодного зайвого (зокрема тих, що завершуються)."""
    t = time.time()
    while time.time() - t < timeout:
        ready, total = ready_web()
        if ready == n and total == n:
            return time.time() - t
        time.sleep(1)
    raise TimeoutError(f"не дочекалась {n} готових подів web: {ready_web()}")


def scale(n):
    kubectl("scale", "deployment/web", f"--replicas={n}")
    return wait_web(n)


def hpa_delete():
    kubectl("delete", "hpa", "web", "--ignore-not-found", check=False)


def web_pods():
    pods = kjson("get", "pods", "-l", "app=web")["items"]
    return [{"pod": p["metadata"]["name"], "node": p["spec"].get("nodeName"),
             "phase": p["status"].get("phase")} for p in pods if not p["metadata"].get("deletionTimestamp")]
