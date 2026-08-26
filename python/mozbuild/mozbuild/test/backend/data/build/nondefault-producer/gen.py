# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/


def main(output, input_path):
    with open(output.name + ".runs", "a") as runs:
        runs.write("run\n")
    with open(input_path) as fh:
        output.write(fh.read())
