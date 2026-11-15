#!/usr/bin/env python3
"""Build an isolated Cetus JAR with corrected targeted optimization modes.

The input compiler is read-only. ReductionTransform is rebuilt. For induction
and interchange, only the corrected method is transplanted into the deployed
class; unrelated methods retain their original bytecode. Requires Java 8/JDK.
"""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, log, *, expected_success=True, timeout=120):
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        log.write_text(output + "\nTIMEOUT after %d seconds\n" % timeout)
        if expected_success:
            raise RuntimeError("Command timed out: " + repr(command)) from error
        return 124
    log.write_text(completed.stdout)
    if expected_success and completed.returncode:
        raise RuntimeError("Command failed: " + repr(command) + "\n" + completed.stdout)
    return completed.returncode


def patch_source(source):
    declaration = "    private static int option;"
    if source.count(declaration) != 1:
        raise ValueError("Expected one original static ReductionTransform.option declaration")
    read_option = """        try {
            option = Integer.parseInt(Driver.getOptionValue("reduction"));
        } catch (Exception ex) {
            option = SCALAR_REDUCTION;
        } finally {
            if (option < 1 || option > 4)
                option = SCALAR_REDUCTION;
        }
"""
    if source.count(read_option) != 1:
        raise ValueError("Original reduction-option initializer did not match")
    constructor_marker = "        work_list = new LinkedList<ForLoop>();\n"
    if source.count(constructor_marker) != 1:
        raise ValueError("Expected one reduction-transform constructor")
    patched = source.replace(declaration, "    private final int option;")
    patched = patched.replace(read_option, "")
    constructor_read = """        // Each target loop can request a different reduction strategy.
        int requestedOption;
        try {
            requestedOption = Integer.parseInt(Driver.getOptionValue("reduction"));
        } catch (Exception ex) {
            requestedOption = SCALAR_REDUCTION;
        }
        option = requestedOption < 1 || requestedOption > 4
                ? SCALAR_REDUCTION : requestedOption;
"""
    return patched.replace(constructor_marker, constructor_marker + constructor_read)


def patch_induction(source):
    signature = "public IVSubstitution(Program program, String loopName, String validOptInvidiual) {"
    if source.count(signature) != 1:
        raise ValueError("Expected one targeted induction constructor")
    start = source.index(signature)
    body = source.index("super(program);", start)
    if "}" in source[start:body]:
        raise ValueError("Targeted induction constructor structure changed")
    return source[:body] + "this(program);" + source[body + len("super(program);"):]


def patch_interchange(source):
    signature = "public void interchangeLoop(LinkedList<Loop> loops, List<Expression> targetOrder, HashMap<ForLoop, String> loopMap) {"
    if source.count(signature) != 1:
        raise ValueError("Expected one explicit interchange method")
    start = source.index(signature)
    body_start = source.index("{", start)
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                    lambda match: " " * len(match.group()), source, flags=re.S)
    depth, end = 1, body_start + 1
    while depth and end < len(source):
        depth += (masked[end] == "{") - (masked[end] == "}")
        end += 1
    if depth:
        raise ValueError("Unbalanced interchange method")
    replacement = '''public void interchangeLoop(LinkedList<Loop> loops, List<Expression> targetOrder, HashMap<ForLoop, String> loopMap) {
      if (loops.isEmpty() || targetOrder.size() != loops.size()) {
         throw new IllegalArgumentException("Target order must name every loop exactly once");
      }
      List<String> current = new ArrayList<String>();
      List<String> target = new ArrayList<String>();
      for (Loop loop : loops) current.add(LoopTools.getIndexVariable(loop).toString());
      for (Expression index : targetOrder) target.add(index.toString());
      if (new LinkedHashSet<String>(current).size() != loops.size()
            || new LinkedHashSet<String>(target).size() != loops.size()
            || !new LinkedHashSet<String>(current).equals(new LinkedHashSet<String>(target))) {
         throw new IllegalArgumentException("Target order is not a permutation of this nest");
      }
      ForLoop outer = (ForLoop) loops.getFirst();
      if (current.equals(target)) {
         loopMap.put(outer, "AlreadyInOrder");
         return;
      }
      // Legality is checked by start(). At most n-1 header swaps realize any order.
      for (int position = 0; position < loops.size(); position++) {
         int selected = current.indexOf(target.get(position));
         if (selected != position) {
            swapLoop((ForLoop) loops.get(position), (ForLoop) loops.get(selected));
            Collections.swap(current, position, selected);
         }
      }
      List<String> actual = new ArrayList<String>();
      for (Loop loop : loops) actual.add(LoopTools.getIndexVariable(loop).toString());
      if (!actual.equals(target)) throw new IllegalStateException("Interchange failed to realize target order");
      loopMap.put(outer, "Permuted");
      System.out.println("[LoopInterchange] loops have been interchanged");
   }'''
    return source[:start] + replacement + source[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_compiler", type=Path)
    parser.add_argument("output_compiler", type=Path)
    args = parser.parse_args()
    source_root = args.source_compiler.resolve()
    output_root = args.output_compiler.resolve()
    if output_root.exists():
        raise ValueError("Output path already exists; choose a fresh compiler directory")
    original_jar = source_root / "lib/cetus.jar"
    patches = {"ReductionTransform": patch_source, "IVSubstitution": patch_induction,
               "LoopInterchange": patch_interchange}
    output_root.mkdir(parents=True)
    (output_root / "lib").mkdir()
    (output_root / "bin").mkdir()
    (output_root / "logs").mkdir()
    (output_root / "classes").mkdir()
    source_files = []
    source_hashes = {}
    for name, patcher in patches.items():
        relative = Path("src/cetus/transforms/" + name + ".java")
        original_source = (source_root / relative).read_text()
        patched_source = patcher(original_source)
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(patched_source)
        source_files.append(destination)
        source_hashes[name] = {"original": sha256(source_root / relative), "patched": sha256(destination)}
        (output_root / (name + ".patch")).write_text("".join(difflib.unified_diff(
            original_source.splitlines(keepends=True), patched_source.splitlines(keepends=True),
            fromfile="a/" + str(relative), tofile="b/" + str(relative))))
    harnesses = ("ReductionOptionRegression", "InductionOptionRegression", "InterchangeRegression")
    for name in (*harnesses, "MethodTransplant"):
        shutil.copy2(Path(__file__).with_name(name + ".java"), output_root / (name + ".java"))
    for jar in (source_root / "lib").glob("*.jar"):
        if jar.name != "cetus.jar":
            shutil.copy2(jar, output_root / "lib" / jar.name)
    original_classpath = os.pathsep.join(str(p) for p in sorted((source_root / "lib").glob("*.jar")))
    classes = output_root / "classes"
    logs = output_root / "logs"
    run(["javac", "-source", "8", "-target", "8", "-XDignore.symbol.file", "-cp", original_classpath,
         "-d", str(classes), *[str(output_root / (name + ".java"))
                               for name in (*harnesses, "MethodTransplant")]], logs / "harness_build.log")
    old_status = run(["java", "-Xmx256m", "-cp", str(classes) + os.pathsep + original_classpath,
                      "ReductionOptionRegression"], logs / "original_regression.log",
                     expected_success=False)
    if old_status == 0:
        raise RuntimeError("Original JAR already passes; do not apply an unneeded patch")
    if "expected 2, got 1" not in (logs / "original_regression.log").read_text():
        raise RuntimeError("Original regression failed for an unexpected reason")
    old_induction = run(["java", "-Xmx256m", "-cp", str(classes) + os.pathsep + original_classpath,
                         "InductionOptionRegression"], logs / "original_induction_regression.log",
                        expected_success=False)
    if old_induction == 0 or "expected 3, got 0" not in (logs / "original_induction_regression.log").read_text():
        raise RuntimeError("Original induction regression did not expose the expected bug")
    old_interchange = run(["java", "-Xmx256m", "-cp", str(classes) + os.pathsep + original_classpath,
                           "InterchangeRegression"], logs / "original_interchange_regression.log",
                          expected_success=False, timeout=5)
    if old_interchange != 124:
        raise RuntimeError("Original interchange regression did not reproduce the known nontermination")
    run(["javac", "-source", "8", "-target", "8", "-cp", original_classpath,
         "-d", str(classes), *[str(path) for path in source_files]], logs / "compiler_build.log")
    replacements = {"cetus/transforms/ReductionTransform.class": classes / "cetus/transforms/ReductionTransform.class"}
    methods = {"IVSubstitution": ("<init>", "(Lcetus/hir/Program;Ljava/lang/String;Ljava/lang/String;)V"),
               "LoopInterchange": ("interchangeLoop", "(Ljava/util/LinkedList;Ljava/util/List;Ljava/util/HashMap;)V")}
    for name, (method, descriptor) in methods.items():
        entry = "cetus/transforms/" + name + ".class"
        destination = output_root / "transplanted" / entry
        run(["java", "-Xmx256m", "-cp", str(classes), "MethodTransplant", str(original_jar), entry,
             str(classes / entry), str(destination), method, descriptor], logs / (name + "_transplant.log"))
        replacements[entry] = destination
    output_jar = output_root / "lib/cetus.jar"
    with zipfile.ZipFile(original_jar) as before, zipfile.ZipFile(output_jar, "w") as after:
        if not set(replacements).issubset(before.namelist()):
            raise RuntimeError("Original JAR is missing a required target class")
        for member in before.infolist():
            content = (replacements[member.filename].read_bytes() if member.filename in replacements
                       else before.read(member.filename))
            after.writestr(member, content)
    with zipfile.ZipFile(original_jar) as before, zipfile.ZipFile(output_jar) as after:
        if before.namelist() != after.namelist():
            raise RuntimeError("Compiler JAR member list changed unexpectedly")
        changed = [name for name in before.namelist() if before.read(name) != after.read(name)]
        if set(changed) != set(replacements):
            raise RuntimeError("Unexpected compiler changes: " + repr(changed))
    # Put JAR before classes so the test exercises the actual delivered artifact.
    patched_classpath = os.pathsep.join(str(p) for p in sorted((output_root / "lib").glob("*.jar")))
    for harness in harnesses:
        run(["java", "-Xmx256m", "-cp", patched_classpath + os.pathsep + str(classes), harness],
            logs / ("patched_" + harness + ".log"), timeout=30)
    wrapper = output_root / "bin/cetus"
    wrapper.write_text('''#!/bin/sh
CETUS_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd) || exit 1
exec java -cp "$CETUS_ROOT/lib/cetus.jar:$CETUS_ROOT/lib/rsyntaxtextarea.jar" -Xmx1g cetus.exec.Driver "$@"
''')
    wrapper.chmod(0o755)
    manifest = {
        "source_compiler": str(source_root),
        "source_jar_sha256": sha256(original_jar),
        "patched_jar_sha256": sha256(output_jar),
        "source_files_sha256": source_hashes,
        "changed_jar_entries": changed,
        "method_transplants": methods,
        "original_regression_exit_code": old_status,
        "original_induction_regression_exit_code": old_induction,
        "original_interchange_regression_exit_code": old_interchange,
        "patched_regression_exit_code": 0,
        "patched_regressions": list(harnesses),
        "cetus_executable": str(wrapper),
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
