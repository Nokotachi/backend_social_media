class AuthError(Exception): ...


class EmailAlreadyRegistered(AuthError):
    pass


class UsernameAlreadyTaken(AuthError):
    pass


class UserNotFound(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class AccountNotActive(AuthError):
    def __init__(self, email: str | None = None) -> None:
        self.email = email
        super().__init__()


class AccountBlocked(AuthError): ...


class AccountAlreadyActive(AuthError): ...


class InvalidOrExpiredCode(AuthError):
    pass


class TooManyAttempts(AuthError):
    pass


class ResendCooldownActive(AuthError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Повторная отправка возможна через {retry_after_seconds} сек.")


class InvalidOrExpiredToken(AuthError): ...


class TooManyLoginAttempts(AuthError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Вход заблокирован на {retry_after_seconds} сек.")


class WrongCurrentPassword(AuthError): ...
