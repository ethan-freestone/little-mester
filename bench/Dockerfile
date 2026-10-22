FROM ghcr.io/ggml-org/llama.cpp:server-cuda

# The benchmark script (HTTP-based, works against any running server)
COPY bench.py /bench/bench.py

# Entry point: if MODEL_DIR is set, run native llama.cpp --test (fast, no reload)
# Otherwise run the HTTP-based benchmark against a running server
ENTRYPOINT ["/bench/entrypoint.sh"]
