#!/usr/bin/env python3
"""Повне видалення кластера лабораторної 3 разом із томами: python scripts/k8s_down.py"""
import subprocess
import sys

sys.exit(subprocess.run(["k3d", "cluster", "delete", "lab3"]).returncode)
