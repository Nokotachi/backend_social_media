#!/bin/bash
# Создаёт по отдельной базе и отдельному пользователю Postgres на каждый сервис
# и накатывает их схемы из /schemas. Выполняется один раз, при первом запуске
# с пустым томом (так работает docker-entrypoint-initdb.d в образе postgres).
#
#   user_db            — User Service            (роль user_service)
#   content_db         — Content Service         (роль content_service)
#   recommendation_db  — Recommendation Service  (роль recommendation_service)
#
# Каждая роль владеет только своей базой; подключиться к чужой не может.
set -euo pipefail

create_service_db() {
    local role="$1" db="$2" password="$3" schema="$4"
    echo "init: ${db} (owner ${role})"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
		CREATE ROLE ${role} LOGIN PASSWORD '${password}';
		CREATE DATABASE ${db} OWNER ${role};
		REVOKE ALL ON DATABASE ${db} FROM PUBLIC;
	SQL
    psql -v ON_ERROR_STOP=1 --username "$role" --dbname "$db" -f "$schema"
}

create_service_db user_service           user_db           "$USER_DB_PASSWORD"           /schemas/user_service.sql
create_service_db content_service        content_db        "$CONTENT_DB_PASSWORD"        /schemas/content_service.sql
create_service_db recommendation_service recommendation_db "$RECOMMENDATION_DB_PASSWORD" /schemas/recommendation_service.sql
