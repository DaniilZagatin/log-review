"""Optional local tokenizer accounting; model context includes input AND maximum output.

Only tokenizer JSON data is loaded. No network, remote model code, truncation or padding. Chat framing varies
by provider: an explicit reserve is added to the text estimate; API prompt usage remains authoritative.
"""
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
from pathlib import Path


@dataclass(frozen=True)
class TokenBudget:
    context_tokens: int
    output_tokens: int
    framing_reserve: int
    tokenizer_sha256: str
    count: object = field(repr=False, compare=False)

    def __post_init__(self):
        if (any(type(value) is not int for value in
                (self.context_tokens, self.output_tokens, self.framing_reserve)) or
                self.output_tokens < 1 or self.framing_reserve < 0 or
                self.context_tokens <= self.output_tokens + self.framing_reserve):
            raise ValueError('Context must leave positive input space after output and framing reserves')
        if (type(self.tokenizer_sha256) is not str or len(self.tokenizer_sha256) != 64 or
                any(ch not in '0123456789abcdef' for ch in self.tokenizer_sha256) or not callable(self.count)):
            raise ValueError('Token budget needs a tokenizer identity and counter')

    def identity(self):
        return {'context_tokens': self.context_tokens, 'max_output_tokens': self.output_tokens,
                'framing_reserve_tokens': self.framing_reserve, 'tokenizer_sha256': self.tokenizer_sha256,
                'input_token_limit': self.context_tokens - self.output_tokens - self.framing_reserve,
                'accounting': 'local text estimate plus explicit framing reserve; API usage authoritative'}

    def measure(self, system, user):
        text = self.count(system) + self.count(user)
        return {**self.identity(), 'text_tokens': text,
                'estimated_total_tokens': text + self.output_tokens + self.framing_reserve}

    def fits(self, system, user, *, extra_tokens=0):
        return self.measure(system, user)['estimated_total_tokens'] + extra_tokens <= self.context_tokens

    @classmethod
    def from_file(cls, path, *, context_tokens, output_tokens, framing_reserve=8192):
        from .source_tools import blocked_path
        path = Path(path).absolute()
        if (blocked_path(path.as_posix()) or any(p.is_symlink() for p in (path, *path.parents)) or
                not path.is_file() or path.stat().st_size > 64 * 1024 * 1024):
            raise ValueError('Tokenizer must be a regular, non-secret JSON data file of at most 64 MiB')
        try:
            from tokenizers import Tokenizer
        except ImportError:
            raise ValueError('Token accounting requires the optional tokenizers dependency') from None
        data = path.read_bytes()
        try:
            tokenizer = Tokenizer.from_str(data.decode('utf-8'))
            tokenizer.no_truncation()
            tokenizer.no_padding()
        except Exception:
            raise ValueError('Invalid tokenizer JSON data') from None

        @lru_cache(maxsize=2)
        def count(text):
            return len(tokenizer.encode(text, add_special_tokens=False).ids)

        return cls(context_tokens, output_tokens, framing_reserve, hashlib.sha256(data).hexdigest(), count)
