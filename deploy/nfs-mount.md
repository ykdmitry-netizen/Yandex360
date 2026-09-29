# Монтирование NFS с хранилищем снимков (/mnt/Mailbox)

Хранилище снимков яндекс-ящиков лежит на NAS: `10.10.1.18:/volume1/Mailbox`.
Консоль обязана видеть его при старте, иначе `Storage.__init__` создаст каталог
на локальном диске и снимки молча уйдут не туда.

## Строка в /etc/fstab

```
10.10.1.18:/volume1/Mailbox /mnt/Mailbox nfs _netdev,x-systemd.automount,x-systemd.mount-timeout=30,noauto 0 0
```

Что здесь важно:

* `x-systemd.automount` — каталог поднимется сам после перезагрузки NAS или
  сервера (раньше приходилось монтировать руками, и один раз снимок едва не
  ушёл на локальный диск);
* `x-systemd.mount-timeout=30` — не ждать вечно, если NAS недоступен;
* `noauto` — не монтировать на этапе загрузки, а по первому обращению.

## Чего здесь быть НЕ должно

**`x-systemd.idle-timeout`.** С ним произошёл реальный сбой 29.09.2026:

```
11:45:09 systemd[1]: Stopping y360-admin.service     ← консоль остановлена
11:45:09 systemd[1]: Unmounting mnt-Mailbox.mount    ← и NFS отмонтирован
```

Через 10 минут простоя automount снял монтирование, а так как в юните
`y360-admin.service` стоит `RequiresMountsFor=/mnt/Mailbox` (сознательная
защита «не работать без хранилища»), systemd корректно остановил консоль.
Пользователь увидел «сервис упал», хотя падения не было — была зависимость.
Итог: без idle-timeout монтирование не снимается само, защита остаётся.

## Проверка после правки fstab

```bash
sudo systemctl daemon-reload
sudo systemctl restart mnt-Mailbox.automount
sudo systemctl restart y360-admin
findmnt /mnt/Mailbox          # должно быть nfs4 с 10.10.1.18
ls /mnt/Mailbox/8365150 | head
```
