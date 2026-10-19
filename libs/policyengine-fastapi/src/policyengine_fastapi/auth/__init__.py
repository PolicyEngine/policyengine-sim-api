"""Bearer-token authentication dependencies for FastAPI routes."""

from .jwt_decoder import JWTDecoder

__all__ = ["JWTDecoder"]
