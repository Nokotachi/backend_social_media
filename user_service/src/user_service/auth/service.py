from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from user_service.auth import login_guard, security
from user_service.auth.exceptions import (
    AccountAlreadyActive,
    AccountBlocked,
    AccountNotActive,
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
from user_service.auth.models import (
    EmailToken,
    EmailTokenPurpose,
    PasswordResetToken,
    RefreshToken,
    User,
    UserStatus,
)
from user_service.auth.schemas import TokenPairResponse
from user_service.core.email import EmailSender
from user_service.settings import Settings
from user_service.users import events
from user_service.users.models import Profile


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def register(
    db: AsyncSession,
    *,
    email: str,
    username: str,
    password: str,
    settings: Settings,
    email_sender: EmailSender,
) -> User:
    existing_email = await db.scalar(select(User).where(User.email == email))
    if existing_email is not None:
        raise EmailAlreadyRegistered()

    existing_username = await db.scalar(
        select(User).where(func.lower(User.username) == username.lower())
    )
    if existing_username is not None:
        raise UsernameAlreadyTaken()

    user = User(
        email=email,
        username=username,
        password_hash=security.hash_password(password),
        status=UserStatus.PENDING.value,
        token_version=1,
        created_at=_now(),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "email" in str(exc.orig):
            raise EmailAlreadyRegistered() from exc
        raise UsernameAlreadyTaken() from exc

    profile = Profile(user_id=user.id, display_name=username, username=username)
    db.add(profile)

    code = security.generate_email_code(settings.email_code_length)
    now = _now()
    email_token = EmailToken(
        user_id=user.id,
        purpose=EmailTokenPurpose.VERIFY.value,
        token_hash=security.hash_email_code(code),
        attempts=0,
        created_at=now,
        expires_at=now + timedelta(minutes=settings.email_code_ttl_minutes),
        used_at=None,
    )
    db.add(email_token)

    await db.commit()
    await db.refresh(user)

    await email_sender.send_verification_code(to_email=user.email, code=code)
    return user


async def _check_code_and_consume(
    db: AsyncSession, *, token: EmailToken, code: str, settings: Settings
) -> None:
    now = _now()

    if token.used_at is not None:
        raise InvalidOrExpiredCode()
    if _aware(token.expires_at) < now:
        raise InvalidOrExpiredCode()
    if token.attempts >= settings.email_code_max_attempts:
        raise TooManyAttempts()

    if security.hash_email_code(code) != token.token_hash:
        token.attempts += 1
        if token.attempts >= settings.email_code_max_attempts:
            token.used_at = now
        await db.commit()
        if token.attempts >= settings.email_code_max_attempts:
            raise TooManyAttempts()
        raise InvalidOrExpiredCode()


async def confirm_registration(
    db: AsyncSession, *, email: str, code: str, settings: Settings
) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        raise UserNotFound()
    if user.status == UserStatus.ACTIVE.value:
        raise AccountAlreadyActive()

    token = await db.get(EmailToken, (user.id, EmailTokenPurpose.VERIFY.value))
    if token is None:
        raise InvalidOrExpiredCode()
    await _check_code_and_consume(db, token=token, code=code, settings=settings)

    user.status = UserStatus.ACTIVE.value
    token.used_at = _now()
    await events.emit_user(db, user.id)
    await db.commit()
    await db.refresh(user)
    return user


async def resend_verification_code(
    db: AsyncSession, *, email: str, settings: Settings, email_sender: EmailSender
) -> None:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        raise UserNotFound()
    if user.status == UserStatus.ACTIVE.value:
        raise AccountAlreadyActive()

    now = _now()
    token = await db.get(EmailToken, (user.id, EmailTokenPurpose.VERIFY.value))
    if token is not None:
        elapsed = (now - _aware(token.created_at)).total_seconds()
        cooldown = settings.email_code_resend_cooldown_seconds
        if elapsed < cooldown:
            raise ResendCooldownActive(retry_after_seconds=int(cooldown - elapsed))

    code = security.generate_email_code(settings.email_code_length)
    if token is None:
        token = EmailToken(user_id=user.id, purpose=EmailTokenPurpose.VERIFY.value)
        db.add(token)

    token.token_hash = security.hash_email_code(code)
    token.attempts = 0
    token.created_at = now
    token.expires_at = now + timedelta(minutes=settings.email_code_ttl_minutes)
    token.used_at = None

    await db.commit()
    await email_sender.send_verification_code(to_email=user.email, code=code)


async def _issue_token_pair(
    db: AsyncSession, *, user: User, settings: Settings
) -> TokenPairResponse:
    access_token = security.create_access_token(
        user_id=user.id, role=user.role, token_version=user.token_version
    )

    raw_refresh = security.generate_opaque_token(settings.refresh_token_bytes)
    now = _now()
    refresh_token = RefreshToken(
        user_id=user.id,
        token_hash=security.hash_opaque_token(raw_refresh),
        version=user.token_version,
        created_at=now,
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        revoked_at=None,
    )
    db.add(refresh_token)
    await db.commit()

    return TokenPairResponse(access_token=access_token, refresh_token=raw_refresh)


async def login(
    db: AsyncSession, *, username: str, password: str, settings: Settings
) -> TokenPairResponse:
    user = await db.scalar(
        select(User).where(func.lower(User.username) == username.strip().lower())
    )
    guard_key = login_guard.key_for(user.id if user else None, username)
    retry_after = login_guard.retry_after_seconds(
        guard_key,
        max_failures=settings.login_max_failures,
        window_minutes=settings.login_failure_window_minutes,
    )
    if retry_after is not None:
        raise TooManyLoginAttempts(retry_after_seconds=retry_after)

    if user is None or not security.verify_password(password, user.password_hash):
        login_guard.register_failure(guard_key)
        raise InvalidCredentials()
    login_guard.reset(guard_key)
    if user.status == UserStatus.BLOCKED.value:
        raise AccountBlocked()
    if user.status != UserStatus.ACTIVE.value:
        raise AccountNotActive(email=user.email)

    return await _issue_token_pair(db, user=user, settings=settings)


async def refresh_tokens(
    db: AsyncSession, *, raw_refresh_token: str, settings: Settings
) -> TokenPairResponse:
    token_hash = security.hash_opaque_token(raw_refresh_token)
    token = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if token is None:
        raise InvalidOrExpiredToken()

    now = _now()
    if token.revoked_at is not None or _aware(token.expires_at) < now:
        raise InvalidOrExpiredToken()

    user = await db.get(User, token.user_id)
    if user is None or token.version != user.token_version:
        raise InvalidOrExpiredToken()
    if user.status != UserStatus.ACTIVE.value:
        raise InvalidOrExpiredToken()

    token.revoked_at = now
    await db.commit()

    return await _issue_token_pair(db, user=user, settings=settings)


async def logout(db: AsyncSession, *, raw_refresh_token: str) -> None:
    token_hash = security.hash_opaque_token(raw_refresh_token)
    token = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if token is None or token.revoked_at is not None:
        return
    token.revoked_at = _now()
    await db.commit()


async def logout_all(db: AsyncSession, *, user_id: int) -> None:
    user = await db.get(User, user_id)
    if user is None:
        raise UserNotFound()

    user.token_version += 1
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await events.emit_user(db, user_id)
    await db.commit()


async def _issue_reset_code(db: AsyncSession, *, user: User, settings: Settings) -> str:
    now = _now()
    token = await db.get(EmailToken, (user.id, EmailTokenPurpose.RESET.value))
    if token is not None:
        elapsed = (now - _aware(token.created_at)).total_seconds()
        cooldown = settings.email_code_resend_cooldown_seconds
        if elapsed < cooldown:
            raise ResendCooldownActive(retry_after_seconds=int(cooldown - elapsed))

    code = security.generate_email_code(settings.email_code_length)
    if token is None:
        token = EmailToken(user_id=user.id, purpose=EmailTokenPurpose.RESET.value)
        db.add(token)

    token.token_hash = security.hash_email_code(code)
    token.attempts = 0
    token.created_at = now
    token.expires_at = now + timedelta(minutes=settings.email_code_ttl_minutes)
    token.used_at = None
    await db.commit()
    return code


async def _set_new_password_and_revoke_all(
    db: AsyncSession, *, user: User, new_password: str
) -> None:
    user.password_hash = security.hash_password(new_password)
    user.token_version += 1
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await events.emit_user(db, user.id)
    login_guard.reset(login_guard.key_for(user.id, ""))


async def forgot_password(
    db: AsyncSession, *, email: str, settings: Settings, email_sender: EmailSender
) -> None:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        return
    code = await _issue_reset_code(db, user=user, settings=settings)
    await email_sender.send_password_reset_code(to_email=user.email, code=code)


async def verify_reset_code(db: AsyncSession, *, email: str, code: str, settings: Settings) -> str:
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        raise UserNotFound()

    token = await db.get(EmailToken, (user.id, EmailTokenPurpose.RESET.value))
    if token is None:
        raise InvalidOrExpiredCode()
    await _check_code_and_consume(db, token=token, code=code, settings=settings)
    token.used_at = _now()

    raw_reset_token = security.generate_opaque_token(settings.password_reset_token_bytes)
    now = _now()
    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=security.hash_opaque_token(raw_reset_token),
        created_at=now,
        expires_at=now + timedelta(minutes=settings.password_reset_token_ttl_minutes),
        used_at=None,
    )
    db.add(reset_token)
    await db.commit()
    return raw_reset_token


async def reset_password(
    db: AsyncSession, *, raw_reset_token: str, new_password: str, settings: Settings
) -> None:
    token_hash = security.hash_opaque_token(raw_reset_token)
    reset_token = await db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    if reset_token is None:
        raise InvalidOrExpiredToken()
    now = _now()
    if reset_token.used_at is not None or _aware(reset_token.expires_at) < now:
        raise InvalidOrExpiredToken()

    user = await db.get(User, reset_token.user_id)
    if user is None:
        raise InvalidOrExpiredToken()

    reset_token.used_at = now
    await _set_new_password_and_revoke_all(db, user=user, new_password=new_password)
    await db.commit()


async def request_password_change(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    settings: Settings,
    email_sender: EmailSender,
) -> None:
    guard_key = login_guard.key_for(user.id, "")
    retry_after = login_guard.retry_after_seconds(
        guard_key,
        max_failures=settings.login_max_failures,
        window_minutes=settings.login_failure_window_minutes,
    )
    if retry_after is not None:
        raise TooManyLoginAttempts(retry_after_seconds=retry_after)
    if not security.verify_password(current_password, user.password_hash):
        login_guard.register_failure(guard_key)
        raise WrongCurrentPassword()

    code = await _issue_reset_code(db, user=user, settings=settings)
    await email_sender.send_password_change_code(to_email=user.email, code=code)


async def confirm_password_change(
    db: AsyncSession, *, user: User, code: str, new_password: str, settings: Settings
) -> TokenPairResponse:
    token = await db.get(EmailToken, (user.id, EmailTokenPurpose.RESET.value))
    if token is None:
        raise InvalidOrExpiredCode()
    await _check_code_and_consume(db, token=token, code=code, settings=settings)
    token.used_at = _now()
    await _set_new_password_and_revoke_all(db, user=user, new_password=new_password)
    await db.commit()
    return await _issue_token_pair(db, user=user, settings=settings)
