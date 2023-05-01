import itertools
import math
import multiprocessing
import time
from typing import Any, List, Tuple, Union

import numpy as np

from evalplus.data import to_raw
from evalplus.eval.utils import (
    create_tempdir,
    reliability_guard,
    swallow_io,
    time_limit,
)


# unbiased estimator from https://github.com/openai/human-eval
def estimate_pass_at_k(
    num_samples: Union[int, List[int], np.ndarray],
    num_correct: Union[List[int], np.ndarray],
    k: int,
) -> np.ndarray:
    """
    Estimates pass@k of each problem and returns them in an array.
    """

    def estimator(n: int, c: int, k: int) -> float:
        """
        Calculates 1 - comb(n - c, k) / comb(n, k).
        """
        if n - c < k:
            return 1.0
        return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))

    if isinstance(num_samples, int):
        num_samples_it = itertools.repeat(num_samples, len(num_correct))
    else:
        assert len(num_samples) == len(num_correct)
        num_samples_it = iter(num_samples)

    return np.array(
        [estimator(int(n), int(c), k) for n, c in zip(num_samples_it, num_correct)]
    )


def construct_inputs_sig(inputs: list) -> str:
    str_builder = ""
    for x in inputs:
        if type(x) == str:
            str_builder += f"'{to_raw(x)}',"
        else:
            str_builder += f"{x},"
    return str_builder[:-1]


# oracle for 032
def _poly(xs: list, x: float):
    """
    Evaluates polynomial with coefficients xs at point x.
    return xs[0] + xs[1] * x + xs[1] * x^2 + .... xs[n] * x^n
    """
    return sum([coeff * math.pow(x, i) for i, coeff in enumerate(xs)])


SUCCESS = "success"
FAILED = "failed"
TIMEOUT = "timed out"


def untrusted_check(
    code: str,
    inputs: List[Any],
    entry_point: str,
    expected,
    atol,
    ref_time: List[float],
    fast_check: bool = False,
) -> Tuple[str, np.ndarray]:
    time_limits = [max(0.05, 2 * t) for t in ref_time]
    timeout = min(5, sum(ref_time) + 1)

    def is_floats(x) -> bool:
        # check if it is float; List[float]; Tuple[float]
        if isinstance(x, float):
            return True
        if isinstance(x, (list, tuple)):
            return all(isinstance(i, float) for i in x)
        if isinstance(x, np.ndarray):
            return x.dtype == np.float64 or x.dtype == np.float32
        return False

    def unsafe_execute(atol):
        with create_tempdir():
            # These system calls are needed when cleaning up tempdir.
            import os
            import shutil

            rmtree = shutil.rmtree
            rmdir = os.rmdir
            chdir = os.chdir
            # Disable functionalities that can make destructive changes to the test.
            # allow only 4GB memory usage
            maximum_memory_bytes = 4 * 1024 * 1024 * 1024
            reliability_guard(maximum_memory_bytes=maximum_memory_bytes)
            exec_globals = {}
            try:
                with swallow_io():
                    exec(code, exec_globals)
                    fn = exec_globals[entry_point]
                    for i, inp in enumerate(inputs):
                        try:
                            with time_limit(time_limits[i]):
                                out = fn(*inp)

                            exp = expected[i]
                            exact_match = out == exp

                            if "find_zero" == entry_point:
                                assert _poly(*out, inp) <= atol

                            if atol == 0 and is_floats(exp):
                                atol = 1e-6  # enforce atol for float comparison
                            if not exact_match and atol != 0:
                                np.testing.assert_allclose(out, exp, atol=atol)
                            else:
                                assert exact_match
                        except BaseException:
                            if fast_check:
                                raise
                            details.append(False)
                            continue

                        details.append(True)
                result.append(SUCCESS)
            except BaseException:
                result.append(FAILED)
            # Needed for cleaning up.
            shutil.rmtree = rmtree
            os.rmdir = rmdir
            os.chdir = chdir

    manager = multiprocessing.Manager()

    result = manager.list()
    details = manager.list()
    p = multiprocessing.Process(target=unsafe_execute, args=(atol,))
    p.start()
    p.join(timeout=timeout + 1)
    if p.is_alive():
        p.terminate()
        time.sleep(0.1)
    if p.is_alive():
        p.kill()
        time.sleep(0.1)
    p.close()

    if not result:
        result.append(TIMEOUT)

    if result[0] == SUCCESS:
        if len(details) != len(inputs) or not all(details):
            result[0] = FAILED

    return result[0], np.array(details)


def evaluate_files(
    files: List[str],
    inputs: List,
    expected: List,
    entry_point: str,
    atol: float,
    ref_time: List[float],
    fast_check: bool = False,
) -> List[Tuple[str, List[bool]]]:
    ret = []
    # sort files by the id in name (i.e., "../n.py")
    files = sorted(files, key=lambda x: int(x.split("/")[-1].split(".")[0]))
    for file in files:
        code = open(file, "r").read()
        stat, det = untrusted_check(
            code,
            inputs,
            entry_point,
            expected=expected,
            atol=atol,
            ref_time=ref_time,
            fast_check=fast_check,
        )
        ret.append((stat, det.tolist()))
    return ret