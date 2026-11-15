#!/usr/bin/env python3
"""Compiler integration check using a separate, supported constant-bound kernel.

This fixture does not alter BT or its eligibility decisions. It verifies actual
Driver discovery -> explicit order request -> generated C -> numerical output.
Run in a compute allocation, not on the login node.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess


HEADER = """#include <stdio.h>
#define TimeInstrument_Tuning_section_kernel_S0 1
extern int timeron;
void timer_start(int);
void timer_stop(int);
"""
SOURCE = """#include "fixture.h"
int timeron = 0;
int values[2][3][4][5];
void timer_start(int which) { }
void timer_stop(int which) { }
void kernel(void) {
  int i, j, k, m;
  for (i = 0; i < 2; i++) {
    for (j = 0; j < 3; j++) {
      for (k = 0; k < 4; k++) {
        for (m = 0; m < 5; m++) {
          values[i][j][k][m] = ((i * 3 + j) * 4 + k) * 5 + m;
        }
      }
    }
  }
}
int main(void) {
  int i, j, k, m, expected;
  unsigned long checksum = 0;
  kernel();
  for (i = 0; i < 2; i++) {
    for (j = 0; j < 3; j++) {
      for (k = 0; k < 4; k++) {
        for (m = 0; m < 5; m++) {
          expected = ((i * 3 + j) * 4 + k) * 5 + m;
          if (values[i][j][k][m] != expected) return 2;
          checksum += values[i][j][k][m] * (expected + 1);
        }
      }
    }
  }
  printf("CHECKSUM=%lu\\n", checksum);
  return 0;
}
"""
BASE_FLAGS = ["-tinline=0", "-teliminate-branch=0", "-induction=0", "-privatize=0",
              "-reduction=0", "-parallelize-loops=0", "-alias=2", "-ddt=2",
              "-ompGen=0", "-profitable-omp=0"]


def command(args, cwd, prefix, timeout=60):
    result = subprocess.run([str(arg) for arg in args], cwd=str(cwd), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    Path(str(prefix) + ".out").write_text(result.stdout)
    Path(str(prefix) + ".json").write_text(json.dumps({
        "command": [str(arg) for arg in args], "cwd": str(cwd), "returncode": result.returncode
    }, indent=2) + "\n")
    if result.returncode:
        raise RuntimeError("Command failed; see " + str(prefix) + ".out")
    return result.stdout


def fixture(path):
    path.mkdir(parents=True)
    (path / "fixture.h").write_text(HEADER)
    (path / "kernel.c").write_text(SOURCE)


def emitted_order(text):
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
    match = re.search(r"\bvoid\s+kernel\s*\([^)]*\)\s*\{", text)
    if not match:
        raise RuntimeError("No generated kernel definition")
    depth, stop = 1, match.end()
    while depth:
        depth += (text[stop] == "{") - (text[stop] == "}")
        stop += 1
    indices = re.findall(r"\bfor\s*\(\s*([ijkm])\s*=", text[match.end():stop])
    return "_".join(indices)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("compiler_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    compiler = args.compiler_root.resolve() / "bin/cetus"
    output = args.output_dir.resolve()
    if output.exists():
        raise ValueError("Choose a fresh fixture output directory")
    output.mkdir(parents=True)
    baseline = output / "serial"
    fixture(baseline)
    command(["gcc", "-O3", "kernel.c", "-o", "program"], baseline, baseline / "compile")
    expected = command([baseline / "program"], baseline, baseline / "execute")
    if expected.strip() != "CHECKSUM=575960":
        raise RuntimeError("Unexpected serial fixture checksum")
    discovery = output / "discovery"
    fixture(discovery)
    log = command([compiler, *BASE_FLAGS, "-validity-opt=kernel#0",
                   "-loop_interchange=validity", "kernel.c"], discovery, discovery / "compiler")
    matches = re.findall(r"LoopName:\s+kernel#0\s+Legal Loop Interchange Transformations:\s*\[([^\]]*)\]", log)
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one legal-order discovery report")
    orders = ["_".join(item.strip().split()) for item in matches[0].split(",")]
    all_orders = {"_".join(order) for order in itertools.permutations("ijkm")}
    if len(orders) != 24 or set(orders) != all_orders:
        raise RuntimeError("Constant-bound independent kernel should expose all 24 orders")
    results = []
    for order in orders:
        directory = output / ("order_" + order)
        fixture(directory)
        payload = ("-Tuning_Analysis=1;kernel>S0<kernel#0%alias=2,tinline=0,"
                   "induction=0,privatize=0,parallelize-loops=0,reduction=0,loop_interchange=" + order)
        command([compiler, *BASE_FLAGS, "-loop_interchange=0", payload, "kernel.c"],
                directory, directory / "compiler")
        generated = directory / "cetus_output/kernel.c"
        actual = emitted_order(generated.read_text())
        if actual != order:
            raise RuntimeError("Requested %s but generated %s" % (order, actual))
        command(["gcc", "-O3", "-I", directory, generated, "-o", directory / "program"],
                directory, directory / "compile")
        observed = command([directory / "program"], directory, directory / "execute")
        if observed != expected:
            raise RuntimeError("Numerical fixture result changed for " + order)
        results.append({"requested_order": order, "emitted_order": actual,
                        "checksum": observed.strip(),
                        "generated_c_sha256": hashlib.sha256(generated.read_bytes()).hexdigest()})
    report = {"status": "passed", "fixture": "independent constant-bound 2x3x4x5 array kernel",
              "legal_orders": orders, "verified_orders": results,
              "compiler_jar_sha256": hashlib.sha256((args.compiler_root / "lib/cetus.jar").read_bytes()).hexdigest(),
              "application_executions": 25,
              "note": "Separate compiler fixture; no change to BT eligibility or benchmark sources."}
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("INTERCHANGE_PIPELINE_PASSED: 24 discovered orders emitted and numerically verified", flush=True)


if __name__ == "__main__":
    main()
