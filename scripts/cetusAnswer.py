from pathlib import Path
import sys

PAPER_ROOT = Path(__file__).resolve().parents[2]

def main():
    if len(sys.argv) != 9:
        print("Usage: cetusAnswer.py BENCHMARK APPLICATION FILE_NAME ANSWER CLASS ALGORITHM WINDOW SECTION", file=sys.stderr)
        return 2
    benchmark, application, file_name, answer, class_name, algorithm, window, section = sys.argv[1:]
    if benchmark == "Nasa":
        directory = PAPER_ROOT / "Benchmarks" / "SNU_NPB-1.0.3" / "NPB3.3-SER-C" / application.upper()
    elif benchmark == "Poly":
        directory = PAPER_ROOT / "Benchmarks" / "PolyBenchC-4.2.1" / application.lower()
    else:
        print("Unknown benchmark family: " + benchmark, file=sys.stderr)
        return 2
    if not directory.is_dir():
        print("Benchmark directory does not exist: " + str(directory), file=sys.stderr)
        return 2
    log_dir = directory / "logFiles"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / ("log_" + file_name.lower() + "_" + class_name + "_" + algorithm + "_" + section + "_w_" + window + ".txt")
    with log_file.open("a") as output:
        output.write("----ANSWER-----\n")
        output.write("BEST SET OF OPTIMIZATIONS FOR SECTION: " + section + " = " + answer + "\n")
        output.write("----ANSWER-----\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
