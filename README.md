# Backend social media

| Сервис | Порт | Что делает | База |
|---|---|---|---|
| `user_service` | 8000 | регистрация, вход, профили, подписки, поиск людей | `user_db` |
| `content_service` | 8001 | посты, ленты, лайки, избранное, репосты, жалобы, комментарии | `content_db` |
| Recommendation Service | — | ещё не начат (лента «Для вас»), база уже создаётся | `recommendation_db` |
| MinIO | 9000, консоль 9001 | картинки: бакеты `avatars` и `posts` | — |
| RabbitMQ | 5672, консоль 15672 | события между сервисами | — |

## Как устроены данные

У каждого сервиса своя база и свой пользователь Postgres, таблицы поделены
как в `social_media_documentation/services/ENDPOINTS.md`. Схемы лежат в
`db/schemas/*.sql` (это `social_network_db/init/01_schema.sql`, разрезанный
по сервисам: внешние ключи между базами убраны, остальное без изменений).
Базы создаёт `db/init/00_create_service_databases.sh` при первом запуске.

Чужие таблицы сервисы не читают и друг к другу по HTTP не ходят. Связь —
события в RabbitMQ (topic exchange `social.events`):

| Событие | Кто шлёт | Что внутри | Что делает получатель |
|---|---|---|---|
| `user.updated` | User | id, username, display_name, avatar_url, role, status, token_version | Content обновляет свою копию пользователя (`user_replicas`) |
| `user.deleted` | User | id | Content удаляет все посты, комментарии, лайки и картинки пользователя |
| `follow.changed` | User | follower_id, followee_id, following | Content обновляет копию подписок (`follow_replicas`) для ленты |
| `posts.counted` | Content | user_id, posts_count | User обновляет `posts_count` в профиле |

Поэтому Content Service проверяет токены, блокировки и строит ленты сам, даже
если User Service лежит, и наоборот.

Сообщения не теряются. Событие сначала пишется в таблицу `outbox` в той же
транзакции, что и само изменение, и отправляется в RabbitMQ уже после коммита,
с подтверждением от брокера. Если брокер или получатель недоступен, события
ждут в `outbox` или в durable-очереди и доходят, когда тот поднимется.
Сообщение, которое не удалось обработать дважды, попадает в очередь
`<сервис>.dead`, его видно в консоли RabbitMQ. Повторы и устаревшие события
получатель отбрасывает по номеру события. При каждом старте сервисы заново
отправляют полный снимок своих данных, так что копии выравниваются сами.

## Токены: RS256

Access-токен подписывает User Service приватным ключом, остальные сервисы
проверяют подпись публичным. Приватный ключ есть только у User Service, так
что подделать токен, имея доступ к Content Service, нельзя.

В Docker ключи создаёт одноразовый контейнер `jwt_keys` при первом запуске и
кладёт их в тома: `jwt_private` подключён только к `user_service`,
`jwt_public` — к `content_service`. В `.env` и в git ключей нет. Чтобы
сменить ключи (все пользователи войдут заново):
`docker compose down && docker volume rm backend_social_media_jwt_private backend_social_media_jwt_public && docker compose up -d`.

Без Docker ключи лежат в `backend_social_media/keys/` (в `.gitignore`), создаются
один раз: `cd user_service && poetry run python -m user_service.keygen ../keys`.

Картинки лежат в MinIO. Сервисы сами создают свои бакеты при старте и
открывают их на чтение, фронт берёт картинку прямо по URL вида
`http://localhost:9000/posts/<id поста>/<файл>.jpg`.

## Настройки: один файл `.env`

Все значения задаются в одном месте, в `backend_social_media/.env` (образец —
`.env.example`). Его читают и
`docker compose`, и сервисы при локальном запуске без Docker. У сервисов
своих `.env` нет, а в `docker-compose.yml` нет значений по умолчанию: если
переменной не хватает, compose или сервис остановится и назовёт её.

Сроки и лимиты заданы один раз, в `settings.py` своего сервиса. Чтобы
поменять, допишите переменную в `.env`:

| Сервис | Переменные |
|---|---|
| User Service | `ACCESS_TOKEN_TTL_MINUTES` (30), `REFRESH_TOKEN_TTL_DAYS` (30), `EMAIL_CODE_TTL_MINUTES` (10), `EMAIL_CODE_MAX_ATTEMPTS` (5), `EMAIL_CODE_RESEND_COOLDOWN_SECONDS` (60), `PASSWORD_MIN_LENGTH` (8), `LOGIN_MAX_FAILURES` (5), `LOGIN_FAILURE_WINDOW_MINUTES` (15), `AVATAR_MAX_BYTES` (10 МБ) |
| Content Service | `MAX_IMAGES_PER_POST` (10), `MAX_IMAGE_BYTES` (10 МБ), `ALLOWED_ASPECT_RATIOS` (`1:1,4:5,16:9`), `MIN_IMAGE_SHORT_SIDE` (560) |

## Запуск

```bash
cp .env.example .env    # и впишите почтовый ящик, см. ниже
docker compose up -d --build
```

- Swagger: http://localhost:8000/docs и http://localhost:8001/docs
- MinIO console: http://localhost:9001 (логин и пароль — `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`)
- RabbitMQ: http://localhost:15672 (логин и пароль — `RABBITMQ_USER` / `RABBITMQ_PASSWORD`).
  Пользователь RabbitMQ создаётся при первом запуске; если поменять пароль в
  `.env` позже, удалите том `backend_social_media_rabbitmq_data`.

## Почта

Коды уходят настоящими письмами через ящик из `.env`. Для Gmail:

1. Включите двухэтапную аутентификацию: myaccount.google.com → «Безопасность».
2. Создайте пароль приложения: https://myaccount.google.com/apppasswords.
3. Впишите адрес в `SMTP_USERNAME`, а 16 символов без пробелов в `SMTP_PASSWORD`.
4. Проверьте: `docker compose exec user_service python -m user_service.core.email ваш@адрес`.
   Если письмо не ушло, команда напишет причину (например, неверный пароль).

Яндекс и Mail.ru настраиваются так же, их параметры есть в `.env.example`.
Если почтовый сервер не принял письмо при регистрации, API отвечает
`503 EmailDeliveryFailed`, подробности — в `docker compose logs user_service`.

## База в DBeaver

Postgres открыт только для вашего компьютера: `localhost:DB_PORT` (из `.env`).
Логин и пароль — `POSTGRES_USER` / `POSTGRES_PASSWORD`, база `postgres`. В
DBeaver на вкладке PostgreSQL включите «Show all databases», чтобы видеть
`user_db`, `content_db` и `recommendation_db`.

## Тесты

User Service по умолчанию тестируется на SQLite, Content Service — только на
Postgres. Тестовые базы в запущенном сервере:

```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "create database user_test owner user_service" -c "create database content_test owner content_service"'
docker compose exec db psql -U user_service -d user_test -f /schemas/user_service.sql
docker compose exec db psql -U content_service -d content_test -f /schemas/content_service.sql
```

Дальше — `TEST_DATABASE_URL` из README сервиса (пароль — `USER_DB_PASSWORD` или
`CONTENT_DB_PASSWORD` из `.env`, порт — `DB_PORT`). Остальные настройки тесты
задают сами в `tests/conftest.py` и от `.env` не зависят.

## Документы

- `docs/FRONTEND_API.md` — контракт для фронтенда: авторизация, ручки по экранам, коды ошибок.
- `docs/MVP_REVIEW.md` — что не сходится между схемой, ENDPOINTS.md, use case и BPMN, и что решить.
- `docs/openapi/*.json` — OpenAPI обоих сервисов (для генерации клиента).
