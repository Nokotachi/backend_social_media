import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator

from user_service.settings import get_settings

settings = get_settings()

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,30}$")

NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]


def _validate_password(value: str) -> str:
    if len(value) < settings.password_min_length:
        raise ValueError(f"Пароль должен быть не короче {settings.password_min_length} символов")
    return value


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    username: str = Field(min_length=3, max_length=30)
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not _USERNAME_RE.match(value):
            raise ValueError("username: только латиница, цифры, '_' и '.', 3-30 символов")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_password(value)


class RegisterResponse(BaseModel):
    user_id: int
    email: EmailStr
    message: str = "Код подтверждения отправлен на email"


class ConfirmRegistrationRequest(BaseModel):
    email: NormalizedEmail
    code: str = Field(min_length=1, max_length=16)


class ResendCodeRequest(BaseModel):
    email: NormalizedEmail


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=30)
    password: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: NormalizedEmail


class VerifyResetCodeRequest(BaseModel):
    email: NormalizedEmail
    code: str = Field(min_length=1, max_length=16)


class VerifyResetCodeResponse(BaseModel):
    reset_token: str


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)


class PasswordChangeRequest(BaseModel):
    current_password: str


class PasswordChangeConfirm(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password(value)


class UserMeResponse(BaseModel):
    id: int
    email: EmailStr
    username: str
    role: str
    status: str

    model_config = {"from_attributes": True}
