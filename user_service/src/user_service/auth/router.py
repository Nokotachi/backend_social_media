from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from user_service.auth import schemas, service
from user_service.auth.dependencies import get_current_user
from user_service.auth.exceptions import (
    AccountAlreadyActive,
    AccountBlocked,
    AccountNotActive,
    AuthError,
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidOrExpiredCode,
    InvalidOrExpiredToken,
    ResendCooldownActive,
    TooManyAttempts,
    TooManyLoginAttempts,
    UsernameAlreadyTaken,
    UserNotFound,
    WrongCurrentPassword,
)
from user_service.auth.models import User
from user_service.core.email import EmailSender, get_email_sender
from user_service.database import get_db
from user_service.settings import Settings, get_settings

router = APIRouter(tags=["auth"])


_ERROR_STATUS_MAP: dict[type[AuthError], int] = {
    EmailAlreadyRegistered: status.HTTP_409_CONFLICT,
    UsernameAlreadyTaken: status.HTTP_409_CONFLICT,
    UserNotFound: status.HTTP_404_NOT_FOUND,
    InvalidCredentials: status.HTTP_401_UNAUTHORIZED,
    AccountNotActive: status.HTTP_403_FORBIDDEN,
    AccountBlocked: status.HTTP_403_FORBIDDEN,
    AccountAlreadyActive: status.HTTP_409_CONFLICT,
    InvalidOrExpiredCode: status.HTTP_400_BAD_REQUEST,
    TooManyAttempts: status.HTTP_429_TOO_MANY_REQUESTS,
    InvalidOrExpiredToken: status.HTTP_401_UNAUTHORIZED,
    WrongCurrentPassword: status.HTTP_403_FORBIDDEN,
}


def _raise_http(exc: AuthError) -> None:
    if isinstance(exc, (ResendCooldownActive, TooManyLoginAttempts)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.__class__.__name__,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    if isinstance(exc, AccountNotActive) and exc.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "AccountNotActive", "email": exc.email},
        ) from exc
    http_status = _ERROR_STATUS_MAP.get(type(exc), status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=http_status, detail=exc.__class__.__name__) from exc


@router.post(
    "/auth/register", response_model=schemas.RegisterResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    body: schemas.RegisterRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> schemas.RegisterResponse:
    try:
        user = await service.register(
            db,
            email=body.email,
            username=body.username,
            password=body.password,
            settings=settings,
            email_sender=email_sender,
        )
    except AuthError as exc:
        _raise_http(exc)
    return schemas.RegisterResponse(user_id=user.id, email=user.email)


@router.post("/auth/register/confirm", response_model=schemas.UserMeResponse)
async def confirm_registration(
    body: schemas.ConfirmRegistrationRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> schemas.UserMeResponse:
    try:
        user = await service.confirm_registration(
            db, email=body.email, code=body.code, settings=settings
        )
    except AuthError as exc:
        _raise_http(exc)
    return schemas.UserMeResponse.model_validate(user)


@router.post("/auth/register/resend-code", status_code=status.HTTP_204_NO_CONTENT)
async def resend_code(
    body: schemas.ResendCodeRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> None:
    try:
        await service.resend_verification_code(
            db, email=body.email, settings=settings, email_sender=email_sender
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/login", response_model=schemas.TokenPairResponse)
async def login(
    body: schemas.LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> schemas.TokenPairResponse:
    try:
        return await service.login(
            db, username=body.username, password=body.password, settings=settings
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/refresh", response_model=schemas.TokenPairResponse)
async def refresh(
    body: schemas.RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> schemas.TokenPairResponse:
    try:
        return await service.refresh_tokens(
            db, raw_refresh_token=body.refresh_token, settings=settings
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: schemas.LogoutRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.logout(db, raw_refresh_token=body.refresh_token)


@router.post("/auth/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    try:
        await service.logout_all(db, user_id=current_user.id)
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/password/forgot", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    body: schemas.ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> None:
    try:
        await service.forgot_password(
            db, email=body.email, settings=settings, email_sender=email_sender
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/password/forgot/verify-code", response_model=schemas.VerifyResetCodeResponse)
async def verify_reset_code(
    body: schemas.VerifyResetCodeRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> schemas.VerifyResetCodeResponse:
    try:
        reset_token = await service.verify_reset_code(
            db, email=body.email, code=body.code, settings=settings
        )
    except AuthError as exc:
        _raise_http(exc)
    return schemas.VerifyResetCodeResponse(reset_token=reset_token)


@router.post("/auth/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    body: schemas.ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    try:
        await service.reset_password(
            db,
            raw_reset_token=body.reset_token,
            new_password=body.new_password,
            settings=settings,
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/password/change/request", status_code=status.HTTP_204_NO_CONTENT)
async def request_password_change(
    body: schemas.PasswordChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> None:
    try:
        await service.request_password_change(
            db,
            user=current_user,
            current_password=body.current_password,
            settings=settings,
            email_sender=email_sender,
        )
    except AuthError as exc:
        _raise_http(exc)


@router.post("/auth/password/change", response_model=schemas.TokenPairResponse)
async def confirm_password_change(
    body: schemas.PasswordChangeConfirm,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> schemas.TokenPairResponse:
    try:
        return await service.confirm_password_change(
            db,
            user=current_user,
            code=body.code,
            new_password=body.new_password,
            settings=settings,
        )
    except AuthError as exc:
        _raise_http(exc)
