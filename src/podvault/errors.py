"""Stable error classes and process exit codes."""


class PodvaultError(Exception):
    exit_code = 1


class ConfigurationError(PodvaultError):
    exit_code = 2


class SafetyError(PodvaultError):
    exit_code = 3


class DependencyError(PodvaultError):
    exit_code = 4


class VerificationError(PodvaultError):
    exit_code = 5


class DestinationConflictError(PodvaultError):
    exit_code = 6


class CredentialError(PodvaultError):
    exit_code = 7


class KopiaCommandError(PodvaultError):
    def __init__(self, message, returncode=1, stdout="", stderr=""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
