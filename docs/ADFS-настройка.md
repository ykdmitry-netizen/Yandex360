# Подключение Y360 Admin к единому входу (ADFS)

Документ для администратора ADFS и сетевых администраторов. Приложение уже
умеет OIDC: нужен только Application Group на стороне ADFS и открытые порты.

## Что уже известно о вашем контуре

| Параметр | Значение |
| :-- | :-- |
| IdP | ADFS, `https://sso.kolmar.ru` (172.16.50.82) |
| Discovery | `https://sso.kolmar.ru/adfs/.well-known/openid-configuration` — **отвечает** |
| Авторизация | `https://sso.kolmar.ru/adfs/oauth2/authorize/` |
| Токены | `https://sso.kolmar.ru/adfs/oauth2/token/` |
| Ключи подписи | `https://sso.kolmar.ru/adfs/discovery/keys` |
| Проверено с сервера | доступен, 53 мс |

Консоль работает в режиме `AUTH_MODE=none` (без входа) до тех пор, пока не
заполнены параметры ниже, поэтому переключение ничего не ломает.

## Что нужно сделать в ADFS

**GUI (ADFS 2016+):** Server Manager → Tools → AD FS Management →
Application Groups → **Add Application Group**.

1. Имя группы: `Y360 Admin`.
2. Шаблон: **Server application accessing a web API**.
3. **Server application**:
   - Redirect URI: `https://y360.kolmar.ru/oauth2/callback`
     (если DNS-имя пока не заведено — временно
     `http://10.10.0.132:8080/oauth2/callback`, но по http OIDC менее безопасен);
   - сохранить сгенерированный **Client Secret** и **Client ID**.
4. **Web API**:
   - Identifier: `https://y360.kolmar.ru` (или тот же Client ID);
   - Access control policy: **Permit specific group** → группа администраторов,
     например `GRP-KOLMAR-Y360-Admins`;
   - Issuance Transform Rules → Add Rule → **Send Group Membership as a Claim**
     (claim type `group`) либо Pass Through входящего claim с типом Group —
     чтобы членство приходило в токене.
5. Scope: приложение запрашивает `openid profile email`. Если ADFS не принимает
   `email`, достаточно `openid profile` — логин берётся из `upn`.

**PowerShell (эквивалент):**

```powershell
New-AdfsApplicationGroup -Name "Y360 Admin" -ApplicationGroupIdentifier "Y360-Admin"
$app = Add-AdfsServerApplication -Name "Y360 Admin" `
    -ApplicationGroupIdentifier "Y360-Admin" `
    -RedirectUri "https://y360.kolmar.ru/oauth2/callback" `
    -GenerateClientSecret
Add-AdfsWebApiApplication -Name "Y360 Admin API" `
    -ApplicationGroupIdentifier "Y360-Admin" `
    -Identifier "https://y360.kolmar.ru" `
    -AccessControlPolicyName "Permit specific group" `
    -AccessControlPolicyParameters @{GroupParameter="GRP-KOLMAR-Y360-Admins"}
$app.ClientId        # → OIDC_CLIENT_ID
$app.ClientSecret    # → OIDC_CLIENT_SECRET (показывается один раз)
```

Проверка после настройки (с сервера 10.10.0.132):

```bash
curl -s "https://sso.kolmar.ru/adfs/.well-known/openid-configuration" | head -c 200
```

## Что нужно от сетевых администраторов

**Исходящие с 10.10.0.132** — без первых трёх пунктов проект не работает:

| Куда | Порт | Зачем |
| :-- | :-- | :-- |
| `10.10.0.27` (контроллер домена) | tcp 636 | LDAPS: синхронизация Active Directory |
| `10.10.1.18` (Synology) | tcp 2049 (+111) | NFS: хранилище снимков ящиков `/mnt/Mailbox` |
| `api360.yandex.net` | tcp 443 | Directory API Яндекс 360 |
| `oauth.yandex.ru` | tcp 443 | token-exchange для IMAP |
| `imap.yandex.ru` | tcp 993 | выгрузка почты (снимки) |
| `sso.kolmar.ru` | tcp 443 | вход через ADFS |
| внутренний DNS / NTP | 53, 123 | разрешение имён и точное время |
| `pypi.org`, `files.pythonhosted.org` | tcp 443 | обновление зависимостей (можно закрыть после установки) |

**Входящие на 10.10.0.132:**

| Порт | Откуда | Зачем |
| :-- | :-- | :-- |
| tcp 443 | админские подсети | консоль через nginx с TLS |
| tcp 22 | админские подсети | сопровождение |
| tcp 8080 | — | можно **закрыть**: после nginx приложение слушает только localhost |

**DNS и сертификат:** A-запись `y360.kolmar.ru` → `10.10.0.132` во внутреннем
DNS и сертификат от внутреннего CA (nginx-конфиг — `deploy/nginx-y360.conf`).

## Что заполнить в .env на сервере

```ini
AUTH_MODE=oidc
SESSION_SECRET=<длинная случайная строка: openssl rand -hex 32>
AUTH_COOKIE_SECURE=1                 # после перехода на https через nginx
OIDC_ISSUER=https://sso.kolmar.ru/adfs
OIDC_CLIENT_ID=<Client ID из ADFS>
OIDC_CLIENT_SECRET=<Client Secret из ADFS>
OIDC_REDIRECT_URI=https://y360.kolmar.ru/oauth2/callback
OIDC_SCOPES=openid profile email
OIDC_ADMIN_GROUP=GRP-KOLMAR-Y360-Admins   # пусто = любой доменный пользователь
OIDC_GROUP_CLAIM=group
UI_HOST=127.0.0.1                    # после перевода на nginx
```

После правки — `sudo systemctl restart y360-admin`. Приложение само проверит,
что все обязательные параметры заполнены, и откажется стартовать при опечатке
(лучше явная ошибка, чем открытая наружу консоль).

## Запасной вариант, если SSO задержится

`AUTH_MODE=ldap` — вход логином и паролем домена через LDAP-bind к контроллеру
домена (пароль нигде не хранится, проверяется самим AD). Нужны те же
`OIDC_ADMIN_GROUP` (имя группы AD) и `SESSION_SECRET`; доступ к LDAPS уже есть.
