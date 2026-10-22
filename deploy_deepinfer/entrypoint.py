"""Verify the historical image profile before replacing this process with its API."""
import os

from deploy_deepinfer.config import RECIPE, load, server_argv, verify_runtime


def main():
    recipe = load(RECIPE)
    verify_runtime(recipe)
    port = int(os.environ['LLMDEPLOY_API_PORT'])
    argv = server_argv(recipe, port)
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
