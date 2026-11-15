import os
from pathlib import Path
import subprocess
import sys
import re
import time
import math
import json
import tempfile





PAPER_ROOT = Path(__file__).resolve().parents[2]
CETUS = str(PAPER_ROOT / "CETUS" / "cetus-tuning" / "bin" / "cetus")
#cetus TUNING
#GCC = "/opt/shared/gcc/14.2.0-openacc/bin/gcc"

BENCHMARK =  sys.argv[1]    
APPLICATION_NAME = sys.argv[2]
FILE_NAME3 = sys.argv[3]
TECHNIQUES = sys.argv[4]
CLASS_NAME = sys.argv[5]
ALGORITHM = sys.argv[6]
WINDOWS_SIZE = sys.argv[7]
EXECUTION_TIMES_ID = sys.argv[8]




first_process = None
second_process = None
third_process = None
four_process = None
five_process = None
six_process = None
seven_process = None
eight_process = None

COMPILER="gcc"


RAW_OUTPUT_DIR = (Path(os.environ["TUNING_RAW_OUTPUT_DIR"]).expanduser().resolve()
                  if os.environ.get("TUNING_RAW_OUTPUT_DIR") else None)
MAX_SAMPLE_BATCHES = 8
APPLICATION_TIMEOUT_SECONDS = 120


def save_raw_output(kind, output, **metadata):
    if RAW_OUTPUT_DIR is None:
        return
    RAW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if isinstance(output, str):
        output = output.encode("latin-1", errors="replace")
    with tempfile.NamedTemporaryFile(prefix=kind + "_", suffix=".out",
                                     dir=str(RAW_OUTPUT_DIR), delete=False) as raw:
        raw.write(output or b"")
        name = Path(raw.name)
    record = {"benchmark": BENCHMARK, "application": APPLICATION_NAME,
              "class": CLASS_NAME, "techniques": TECHNIQUES,
              "timer_ids": EXECUTION_TIMES_ID, "working_directory": os.getcwd()}
    record.update(metadata)
    name.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")


def checked_build(command):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, check=True)
    except subprocess.CalledProcessError as error:
        save_raw_output("build", error.stdout, command=command,
                        returncode=error.returncode)
        details = (error.stdout or b"").decode("latin-1", errors="replace")
        raise RuntimeError("Build/transform command failed: " + repr(command) +
                           "\n" + details[-16000:]) from error
    save_raw_output("build", result.stdout, command=command, returncode=0)
    return result


def run_checked_application():
    try:
        result = subprocess.run(["./output"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, check=True,
                                timeout=APPLICATION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        save_raw_output("application", error.stdout, timeout_seconds=APPLICATION_TIMEOUT_SECONDS)
        raise RuntimeError("Application exceeded the 120-second execution limit") from error
    except subprocess.CalledProcessError as error:
        save_raw_output("application", error.stdout, returncode=error.returncode)
        raise RuntimeError("Application exited with status " + str(error.returncode)) from error
    save_raw_output("application", result.stdout, returncode=0)
    output = result.stdout.decode("latin-1", errors="replace")
    if BENCHMARK == "Nasa" and os.environ.get("TUNING_VERIFY_NAS") == "1":
        if not re.search(r"^\s*Verification\s*=\s*SUCCESSFUL\s*$", output, re.MULTILINE):
            raise RuntimeError("NAS candidate did not report Verification = SUCCESSFUL")
    return output


def parse_section_time(output, section):
    matches = re.findall(r"^\s*" + re.escape(section) + r"\s*=\s*(\S+)\s*$",
                         output, re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("Expected one timing for " + section +
                           "; found " + str(len(matches)))
    try:
        measured = float(matches[0])
    except ValueError as error:
        raise RuntimeError("Invalid timing for " + section + ": " + matches[0]) from error
    if not math.isfinite(measured) or measured <= 0.0:
        raise RuntimeError("Timing must be finite and positive for " + section +
                           ": " + matches[0])
    return measured


def compilation():
    global APPLICATION_NAME
    if BENCHMARK == "Nasa":
        os.chdir(PAPER_ROOT / 'Benchmarks' / 'SNU_NPB-1.0.3' / 'NPB3.3-SER-C')
        os.chdir(os.path.join(APPLICATION_NAME))

    else:
        
        os.chdir(PAPER_ROOT / 'Benchmarks' / 'PolyBenchC-4.2.1')
        APPLICATION_NAME = APPLICATION_NAME.lower()
        os.chdir(os.path.join(APPLICATION_NAME))
        
    # Write variables to the file
    if APPLICATION_NAME in ["CG","CG_SPARSE_W1_L0"]:
     # Write variables to the file   
        checked_build(["make", f"clean"])
        checked_build(["make", f"CLASS={CLASS_NAME}"])
        subprocess.run(["rm", "-r", "output", "cetus_cg.c", "cetus_output/cg.c"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        checked_build([CETUS, TECHNIQUES, "cg.c"])
        checked_build(["cp", "cetus_output/cg.c", "cetus_cg.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-mcmodel=large", "-o", "output", "cetus_cg.c", "../common/print_results.c", "../common/randdp.c", "../common/c_timers.c", "../common/wtime.c", "-lm"])
    elif APPLICATION_NAME in ["MG"]:
     # Write variables to the file   
        checked_build(["make", f"clean"])
        checked_build(["make", f"CLASS={CLASS_NAME}"])
        subprocess.run(["rm", "-r", "output", "cetus_mg.c", "cetus_output/mg.c"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        checked_build([CETUS, TECHNIQUES, "mg.c"])
        checked_build(["cp", "cetus_output/mg.c", "cetus_mg.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-mcmodel=large", "-o", "output", "cetus_mg.c", "../common/print_results.c", "../common/randdp.c", "../common/c_timers.c", "../common/wtime.c", "-lm"])

    elif APPLICATION_NAME in ["BT","BT_EXACT_W1_L5","BT_INITIALIZE_X_Y_Z"]:
        subprocess.run(["rm", "-r", "cetus_output", "output", "cetus_bt.c", "cetus_add.c", "cetus_adi.c", "cetus_error.c", "cetus_exact_rhs.c",  "cetus_exact_solution.c", "cetus_initialize.c", "cetus_rhs.c", "cetus_set_constants.c",  "cetus_verify.c", "cetus_x_solve.c", "cetus_y_solve.c", "cetus_z_solve.c" ,"cetus_solve_subs.c", "cetus_output/bt.c", "cetus_output/add.c", "cetus_output/adi.c", "cetus_output/error.c", "cetus_output/exact_rhs.c", "cetus_output/exact_solution.c", "cetus_output/initialize.c", "cetus_output/rhs.c", "cetus_output/set_constants.c", "cetus_output/verify.c", "cetus_output/x_solve.c", "cetus_output/y_solve.c", "cetus_output/z_solve.c", "cetus_output/solve_subs.c"],  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        #subprocess.run(["mkdir","testBT"])
        checked_build(["make", f"CLASS={CLASS_NAME}"])
        checked_build([CETUS, TECHNIQUES, "bt.c", "initialize.c", "exact_solution.c", "exact_rhs.c", "set_constants.c", "adi.c", "rhs.c", "x_solve.c", "y_solve.c", "z_solve.c", "add.c", "error.c", "verify.c", "solve_subs.c"])
        checked_build(["cp", "cetus_output/bt.c", "cetus_bt.c"])
        checked_build(["cp", "cetus_output/add.c", "cetus_add.c"])
        checked_build(["cp", "cetus_output/adi.c", "cetus_adi.c"])
        checked_build(["cp", "cetus_output/error.c", "cetus_error.c"])
        checked_build(["cp", "cetus_output/exact_rhs.c", "cetus_exact_rhs.c"])
        checked_build(["cp", "cetus_output/exact_solution.c", "cetus_exact_solution.c"])
        checked_build(["cp", "cetus_output/initialize.c", "cetus_initialize.c"])
        checked_build(["cp", "cetus_output/rhs.c", "cetus_rhs.c"])
        checked_build(["cp", "cetus_output/set_constants.c", "cetus_set_constants.c"])
        checked_build(["cp", "cetus_output/verify.c", "cetus_verify.c"])
        checked_build(["cp", "cetus_output/x_solve.c", "cetus_x_solve.c"])
        checked_build(["cp", "cetus_output/y_solve.c", "cetus_y_solve.c"])
        checked_build(["cp", "cetus_output/z_solve.c", "cetus_z_solve.c"])
        checked_build(["cp", "cetus_output/solve_subs.c", "cetus_solve_subs.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-mcmodel=large", "-o", "output", "cetus_bt.c", "cetus_solve_subs.c", "cetus_initialize.c", "cetus_exact_solution.c", "cetus_exact_rhs.c", "cetus_set_constants.c", "cetus_adi.c", "cetus_rhs.c", "cetus_x_solve.c", "cetus_y_solve.c", "cetus_z_solve.c", "cetus_add.c", "cetus_error.c", "cetus_verify.c", "../common/print_results.c", "../common/randdp.c", "../common/c_timers.c", "../common/wtime.c", "-lm"])

    elif APPLICATION_NAME in ["SP"]:
        subprocess.run(["rm", "-r", "output","cetus_sp.c", "cetus_add.c", "cetus_adi.c", "cetus_error.c", "cetus_exact_rhs.c", "cetus_exact_solution.c", "cetus_initialize.c", "cetus_ninvr.c", "cetus_pinvr.c", "cetus_rhs.c", "cetus_set_constants.c", "cetus_txinvr.c", "cetus_tzetar.c", "cetus_verify.c", "cetus_x_solve.c", "cetus_y_solve.c", "cetus_z_solve.c"],  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        checked_build([CETUS, TECHNIQUES, "sp.c", "add.c", "adi.c", "error.c", "exact_rhs.c", "exact_solution.c", "initialize.c", "ninvr.c", "pinvr.c", "rhs.c", "set_constants.c", "txinvr.c", "tzetar.c", "verify.c", "x_solve.c", "y_solve.c", "z_solve.c"])
        checked_build(["cp", "cetus_output/sp.c", "cetus_sp.c"])
        checked_build(["cp", "cetus_output/add.c", "cetus_add.c"])
        checked_build(["cp", "cetus_output/adi.c", "cetus_adi.c"])
        checked_build(["cp", "cetus_output/error.c", "cetus_error.c"])
        checked_build(["cp", "cetus_output/exact_rhs.c", "cetus_exact_rhs.c"])
        checked_build(["cp", "cetus_output/exact_solution.c", "cetus_exact_solution.c"])
        checked_build(["cp", "cetus_output/initialize.c", "cetus_initialize.c"])
        checked_build(["cp", "cetus_output/ninvr.c", "cetus_ninvr.c"])
        checked_build(["cp", "cetus_output/pinvr.c", "cetus_pinvr.c"])
        checked_build(["cp", "cetus_output/rhs.c", "cetus_rhs.c"])
        checked_build(["cp", "cetus_output/set_constants.c", "cetus_set_constants.c"])
        checked_build(["cp", "cetus_output/txinvr.c", "cetus_txinvr.c"])
        checked_build(["cp", "cetus_output/tzetar.c", "cetus_tzetar.c"])
        checked_build(["cp", "cetus_output/verify.c", "cetus_verify.c"])
        checked_build(["cp", "cetus_output/x_solve.c", "cetus_x_solve.c"])
        checked_build(["cp", "cetus_output/y_solve.c", "cetus_y_solve.c"])
        checked_build(["cp", "cetus_output/z_solve.c", "cetus_z_solve.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-mcmodel=large", "-o", "output", "cetus_sp.c", "cetus_add.c", "cetus_adi.c", "cetus_error.c", "cetus_exact_rhs.c", "cetus_exact_solution.c", "cetus_initialize.c", "cetus_ninvr.c", "cetus_pinvr.c", "cetus_rhs.c", "cetus_set_constants.c", "cetus_txinvr.c", "cetus_tzetar.c", "cetus_verify.c", "cetus_x_solve.c", "cetus_y_solve.c", "cetus_z_solve.c", "../common/print_results.c", "../common/randdp.c", "../common/c_timers.c", "../common/wtime.c", "-lm"])
        subprocess.run(["rm", "-r", "cetus_sp.c", "cetus_add.c", "cetus_adi.c", "cetus_error.c", "cetus_exact_rhs.c", "cetus_exact_solution.c", "cetus_initialize.c", "cetus_ninvr.c", "cetus_pinvr.c", "cetus_rhs.c", "cetus_set_constants.c", "cetus_txinvr.c", "cetus_tzetar.c", "cetus_verify.c", "cetus_x_solve.c", "cetus_y_solve.c", "cetus_z_solve.c"],  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    
    elif APPLICATION_NAME == "2mm":
        subprocess.run(["rm", "-r", "cetus_output", "output", "cetus_bt.c"],  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        checked_build([CETUS, TECHNIQUES, f"{FILE_NAME3}.c"])
        checked_build(["cp", f"cetus_output/{FILE_NAME3}.c", f"cetus_{FILE_NAME3}.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-DLARGE_DATASET" ,"-I", "utilities" ,"-I", "../2mm" ,"../utilities/polybench.c","../common/randdp.c", "../common/c_timers.c","../common/wtime.c", "cetus_2mm.c", "-o","output"])

    elif APPLICATION_NAME in ["LU", "LU_1", "LU_2", "LU_3"]:
        checked_build(["make", f"clean"])
        checked_build(["make", f"CLASS={CLASS_NAME}"])
        subprocess.run(["rm", "-r", "output", "cetus_lu.c", "cetus_read_input.c", "cetus_domain.c", "cetus_setcoeff.c", "cetus_setbv.c", "cetus_exact.c","cetus_setiv.c","cetus_erhs.c", "cetus_ssor.c", "cetus_rhs.c","cetus_l2norm.c","cetus_jacld.c","cetus_blts.c","cetus_jacu.c","cetus_buts.c", "cetus_error.c","cetus_pintgr.c","cetus_verify.c", "cetus_output"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        checked_build([CETUS, TECHNIQUES, "lu.c", "read_input.c", "domain.c", "setcoeff.c", "setbv.c", "exact.c", "setiv.c", "erhs.c", "ssor.c", "rhs.c", "l2norm.c", "jacld.c", "blts.c", "jacu.c", "buts.c", "error.c", "pintgr.c", "verify.c"])
        checked_build(["cp", "cetus_output/lu.c", "cetus_lu.c"])
        checked_build(["cp", "cetus_output/read_input.c", "cetus_read_input.c"])
        checked_build(["cp", "cetus_output/domain.c", "cetus_domain.c"])
        checked_build(["cp", "cetus_output/setcoeff.c", "cetus_setcoeff.c"])
        checked_build(["cp", "cetus_output/setbv.c", "cetus_setbv.c"])
        checked_build(["cp", "cetus_output/exact.c", "cetus_exact.c"])
        checked_build(["cp", "cetus_output/setiv.c", "cetus_setiv.c"]) 
        checked_build(["cp", "cetus_output/erhs.c", "cetus_erhs.c"])
        checked_build(["cp", "cetus_output/ssor.c", "cetus_ssor.c"])
        checked_build(["cp", "cetus_output/rhs.c", "cetus_rhs.c"])
        checked_build(["cp", "cetus_output/l2norm.c", "cetus_l2norm.c"])
        checked_build(["cp", "cetus_output/jacld.c", "cetus_jacld.c"])
        checked_build(["cp", "cetus_output/blts.c", "cetus_blts.c"])
        checked_build(["cp", "cetus_output/jacu.c", "cetus_jacu.c"])
        checked_build(["cp", "cetus_output/buts.c", "cetus_buts.c"])
        checked_build(["cp", "cetus_output/error.c", "cetus_error.c"])
        checked_build(["cp", "cetus_output/pintgr.c", "cetus_pintgr.c"])
        checked_build(["cp", "cetus_output/verify.c", "cetus_verify.c"])
        checked_build([COMPILER, "-O3", "-fopenmp", "-mcmodel=large", "-o", "output", "cetus_lu.c", "cetus_read_input.c", "cetus_domain.c", "cetus_setcoeff.c", "cetus_setbv.c", "cetus_exact.c","cetus_setiv.c","cetus_erhs.c", "cetus_ssor.c", "cetus_rhs.c","cetus_l2norm.c","cetus_jacld.c","cetus_blts.c","cetus_jacu.c","cetus_buts.c", "cetus_error.c","cetus_pintgr.c","cetus_verify.c","../common/print_results.c", "../common/randdp.c", "../common/c_timers.c", "../common/wtime.c", "-lm"])




    """  result = subprocess.run(["sbatch", COMPILATION_SH,
                        APPLICATION_NAME, CLASS_NAME, "gcc",
                        FILE_NAME3, TECHNIQUES],
                        stdout=subprocess.PIPE, text=True)
    
    if result.returncode != 0:
        print("Failed to submit job:", result.stderr)
        raise SystemExit(1)
    print(result.stdout)
    print(result.stderr)
    sbatch_output = result.stdout.strip()
    print("sbatch output:", sbatch_output)

    # Parse out the job ID (assuming the last token is the ID):
    # e.g., "Submitted batch job 123456"
    job_id_match = re.search(r"Submitted batch job (\d+)", sbatch_output)
    if not job_id_match:
        print("Could not parse job ID from sbatch output.")
        raise SystemExit(1)

    job_id = job_id_match.group(1)
    print(f"Submitted job ID: {job_id}")
    
    while True:
        # Check whether job is still in queue
        squeue_result = subprocess.run(
            ["squeue", "-j", job_id],
            capture_output=True, text=True
        )
        
        # If the job is no longer seen by squeue, break out
        if job_id not in squeue_result.stdout:
            print(f"Job {job_id} has completed or is no longer in the queue.")
            break
        
        print(f"Job {job_id} is still running or pending...")
        time.sleep(10)  # Wait 10 seconds before checking again    """
                        

def section_techniques(section):
    identity = section[len("TimeInstrument_Tuning_section_"):]
    function, section_number = identity.rsplit("_S", 1)
    section_id = "S" + section_number
    for procedure in TECHNIQUES.split(";")[1:]:
        if procedure.split(">", 1)[0] == function:
            match = re.search(re.escape(section_id) + r"<\[(.*?)\]", procedure)
            if match:
                return match.group(1).split("&")
    return []


def measure_section(section, initial_outputs):
    log_dir = Path.cwd() / "logFiles"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / ("log_" + FILE_NAME3 + "_" + CLASS_NAME + "_" + ALGORITHM +
                          "_" + section + "_w_" + WINDOWS_SIZE + ".txt")
    outputs = initial_outputs
    for attempt in range(MAX_SAMPLE_BATCHES):
        if attempt:
            outputs = [run_checked_application() for _ in range(3)]
        samples = [parse_section_time(output, section) for output in outputs]
        mean = sum(samples) / 3.0
        # Preserve the existing rule: strictly within 5% of the three-run mean.
        accepted = [value for value in samples if abs(value - mean) / mean * 100 < 5.0]
        with log_file.open("a") as log:
            for technique in section_techniques(section):
                log.write(technique + "\n")
            log.write("3 runs: " + repr(samples) + "\n")
            if accepted:
                minimum = min(accepted)
                log.write("Runs within 5% of the mean time: " + repr(accepted) + "\n")
                log.write(section + " = " + repr(minimum) + "\n")
                log.write("-----------------------------------------------------------------\n")
                return minimum
            log.write("No runs within 5% of the mean; batch " + str(attempt + 1) +
                      " of " + str(MAX_SAMPLE_BATCHES) + "\n")
            log.write("-----------------------------------------------------------------\n")
    raise RuntimeError("No stable three-run batch for " + section + " after " +
                       str(MAX_SAMPLE_BATCHES) + " attempts")


def runApplication():
    sections = list(filter(None, EXECUTION_TIMES_ID.split(";")))
    if not sections:
        raise RuntimeError("No section timer identifiers were supplied")
    outputs = [run_checked_application() for _ in range(3)]
    for section in sections:
        print("The actual procedure is: " + section)
        measure_section(section, outputs)


def main():
    compilation()
    runApplication()


if __name__ == "__main__":
    main()
