# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/


def main(output):
    with open(output.name + ".runs", "a") as runs:
        runs.write("run\n")
    output.write("consumed\n")
