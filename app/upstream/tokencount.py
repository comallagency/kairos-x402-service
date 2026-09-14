"""Real BPE token counting (cl100k_base, the GPT-3.5/4-family encoding) for
/pdf and /web-read's `token_count` field - not a len(text)/4 approximation.
The encoding file is baked into the Docker image at build time (see
Dockerfile) specifically so this never makes a network call at request time:
tiktoken.get_encoding() downloads on first use otherwise, which would be a
new runtime dependency on OpenAI's CDN for a route that has nothing else to
do with OpenAI.
"""

import tiktoken

_encoding = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding.encode(text, disallowed_special=()))
