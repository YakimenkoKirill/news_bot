# news_bot

Бот собирает новости из RSS-лент, сайтов и открытых Telegram-каналов, пересказывает их через DeepSeek и публикует пересказ в Telegram-канал со ссылкой на источник.

## Быстрый старт

Нужен Python 3.11+.

```bash
make install
make lint
make test
```

## Для разработчиков

Правила проекта, инварианты и команды описаны в [CLAUDE.md](.claude/CLAUDE.md). Шаги разработки лежат в каталоге [prompts/](prompts/README.md).
