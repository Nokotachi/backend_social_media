# User Service

Аутентификация, авторизация и профили. Стек: FastAPI + SQLAlchemy (async,
asyncpg) + Pydantic v2 + PyJWT (RS256) + argon2-cffi + aio-pika, Poetry, Python 3.12.

Реализовано: auth (регистрация, подтверждение email, вход по username, refresh/logout,
сброс пароля, смена пароля с кодом на почту), профили (`/users/me`, `/users/{username}`, редактирование,
аватар, удаление аккаунта), подписки, поиск людей и блокировка
пользователей админом. Контракт для фронтенда — `../docs/FRONTEND_API.md`.

У сервиса своя база `user_db` (users, profiles, follows и токены). Схему он
**не создаёт и не мигрирует**: DDL лежит в `../db/schemas/user_service.sql`,
базу поднимает `docker compose` из `backend_social_media/`. Alembic в проекте
намеренно нет.

С Content Service сервис связан событиями RabbitMQ (`core/bus.py`,
`users/events.py`, список событий — в README в корне): отправляет изменения
пользователей и подписок, получает число постов для профиля. Таблицы
`outbox` и `user_post_counts` сервис при старте создаёт сам, если их нет.

## Структура

```
src/user_service/
├── main.py              — точка входа FastAPI, роутинг
├── settings.py           — конфиг из общего ../.env (pydantic-settings)
├── database.py            — async engine, сессии, Base
├── auth/
│   ├── models.py            — User, RefreshToken, EmailToken, PasswordResetToken
│   ├── schemas.py             — Pydantic-схемы запросов/ответов
│   ├── security.py             — хэширование паролей/токенов, JWT
│   ├── service.py               — вся бизнес-логика auth
│   ├── dependencies.py           — get_current_user (JWT → User)
│   ├── router.py                  — HTTP-обвязка над service.py
│   └── exceptions.py               — доменные исключения
├── users/
│   ├── models.py             — Profile, Follow
│   ├── schemas.py             — профили, списки, поиск
│   ├── service.py              — профили, подписки (со счётчиками), поиск, блокировка
│   ├── router.py                — /users/*, /search/users, /admin/users/*
│   └── events.py                — события для других сервисов и posts_count
├── keygen.py              — создание пары RSA-ключей для JWT
└── core/
    ├── bus.py                — outbox и RabbitMQ: отправка и чтение событий
    ├── email.py              — письма с кодами: SMTP (ящик из .env) или console (в лог)
    ├── pagination.py          — курсорная пагинация
    └── storage.py              — аватары: MinIO (s3) или локальная папка (local)
```

## Запуск локально

Настройки берутся из общего `../.env` (см. README в корне), своего `.env` у
сервиса нет. База, MinIO и RabbitMQ должны быть уже подняты:

```bash
# в backend_social_media/
docker compose up -d db minio rabbitmq
cd user_service
poetry install
poetry run python -m user_service.keygen ../keys   # один раз
poetry run uvicorn user_service.main:app --reload
```

Основные таблицы сервис не создаёт, только таблицы событий (`outbox`,
`user_post_counts`), если их нет.
Письма уходят через ящик из `.env`; чтобы только видеть коды в логе, добавьте
в `.env` строку `EMAIL_BACKEND=console`.

Или весь бэкенд разом: `docker compose up -d --build` в `backend_social_media/`.

## Тесты

```bash
poetry run pytest
```

Чтобы прогнать на настоящем Postgres со схемой команды (ловит то, чего SQLite
не видит, например уникальность по `lower(email)`):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user_service:<USER_DB_PASSWORD>@localhost:<DB_PORT>/user_test poetry run pytest
```

Пароль и порт — из `../.env`. Как создать `user_test` — в README в корне. Остальные настройки тесты задают сами в `tests/conftest.py`.

Тестовая база должна быть отдельной и со схемой, перед каждым тестом её
таблицы очищаются.

Тесты гоняются на in-memory SQLite (`aiosqlite`), без поднятия Postgres, и
перехватывают "отправленные" email-коды через подмену `EmailSender`
(`tests/conftest.py: SpyEmailSender`) — так проверяется весь цикл
регистрация → подтверждение → логин → refresh → logout-all без моков самого
бизнес-слоя.

## Ключевые решения (кратко; разбор MVP — в ../docs/MVP_REVIEW.md)

- **Таблицы — во множественном числе** (`users`, `profiles`, `refresh_tokens`,
  `email_tokens`, `password_reset_tokens`), как в реальной БД — не как в
  более ранних черновиках ERD.
- **Refresh-токены**: в БД хранится только sha256-хэш; выдаётся клиенту один
  раз. Двухслойный отзыв — `users.token_version` (мгновенный массовый отзыв,
  сравнение без похода по каждой строке) + `revoked_at` (точечный отзыв
  одного устройства, основное назначение; также проставляется массово при
  logout-everywhere и смене пароля). Оба стартуют с `1`, чтобы совпадать.
- **Пароли**: argon2 (argon2-cffi), без ручной соли — библиотека сама
  генерирует и хранит соль в составе хэша.
- **Email-коды**: 6 цифр, TTL и лимит попыток — в `settings.py`. У
  `email_tokens` есть настоящий `created_at`, PK составной `(user_id, purpose)`
  — verify- и reset-код могут существовать одновременно.
- **JWT**: RS256, подписывается приватным ключом `JWT_PRIVATE_KEY_FILE`.
  Access-токен живёт 30 минут (AUTH-02), в claims — `role` и
  `token_version` (снапшот на момент выдачи; устаревает не более чем на TTL
  токена — это сознательный компромисс, см. обсуждение в документации).
- **Таймстемпы**: везде `TIMESTAMPTZ`/aware UTC, как в реальной схеме.
