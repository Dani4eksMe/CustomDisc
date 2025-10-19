"""Main entry point for the Telegram support bot."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from .config import CONFIG
from . import database

LOGGER = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "Здравствуйте! Я — бот поддержки. Напишите свой вопрос одним сообщением, "
    "и мы откроем обращение для общения с командой поддержки."
)

TAKE_TICKET_CALLBACK = "ticket_take"
PANEL_CALLBACK = "ticket_panel"
RATING_CALLBACK = "ticket_rating"
ADMIN_CALLBACK = "admin_panel"

AUTO_CLOSE_TIMEOUT = timedelta(hours=24)


def format_user_mention(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


async def ensure_linked_group() -> Optional[int]:
    value = await database.get_setting("linked_group_id")
    return int(value) if value else None


async def is_owner(user: User) -> bool:
    if user.username and user.username.lower() == CONFIG.owner_username.lower():
        return True
    owner_id = await database.get_setting("owner_id")
    return owner_id is not None and int(owner_id) == user.id


async def ensure_support_profile(user: User, *, make_admin: bool = False) -> None:
    member = await database.get_support_member(user.id)
    wage_setting = await database.get_setting("default_ticket_wage")
    wage = int(wage_setting) if wage_setting else CONFIG.default_ticket_wage
    if member is None:
        await database.add_support_member(
            database.SupportMember(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name,
                status="active",
                tickets_resolved=0,
                balance=0,
                total_penalties=0,
                wallet=None,
                is_admin=make_admin,
                wage_per_ticket=wage,
            )
        )
    else:
        if member.username != user.username or member.full_name != user.full_name:
            await database.add_support_member(
                database.SupportMember(
                    user_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                    status=member.status,
                    tickets_resolved=member.tickets_resolved,
                    balance=member.balance,
                    total_penalties=member.total_penalties,
                    wallet=member.wallet,
                    is_admin=member.is_admin or make_admin,
                    wage_per_ticket=member.wage_per_ticket or wage,
                )
            )
        elif make_admin and not member.is_admin:
            await database.add_support_member(
                database.SupportMember(
                    user_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                    status=member.status,
                    tickets_resolved=member.tickets_resolved,
                    balance=member.balance,
                    total_penalties=member.total_penalties,
                    wallet=member.wallet,
                    is_admin=True,
                    wage_per_ticket=member.wage_per_ticket or wage,
                )
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.effective_chat.send_message(WELCOME_MESSAGE)


async def link_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await update.message.reply_text("Команду /link можно использовать только в группе поддержки.")
        return

    bot = context.bot
    member = await bot.get_chat_member(chat.id, user.id)
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.message.reply_text("Только администратор может привязать группу.")
        return

    if not chat.is_forum:
        await update.message.reply_text(
            "Группа должна быть преобразована в форум (включите Темы) чтобы бот мог создавать обращения."
        )
        return

    await database.set_setting("linked_group_id", str(chat.id))
    await update.message.reply_text("Группа успешно привязана к боту поддержки.")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if context.user_data.get("awaiting_wallet"):
        # Сообщение ожидается как новые реквизиты, обработается в collect_wallet.
        return

    if await database.is_banned(user.id):
        await message.reply_text(
            "Ваш доступ к поддержке ограничен. Если вы считаете это ошибкой, напишите @{}".format(CONFIG.owner_username)
        )
        return

    group_id = await ensure_linked_group()
    if not group_id:
        await message.reply_text(
            "Служба поддержки временно недоступна. Пожалуйста, попробуйте позже."
        )
        return

    ticket = await database.get_open_ticket_for_user(user.id)
    bot = context.bot
    if ticket is None:
        topic_id = await create_ticket_topic(bot, group_id, user)
        ticket_id = await database.create_ticket(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            group_id=group_id,
            topic_id=topic_id,
        )
        await schedule_auto_close(ticket_id, context)
        await send_ticket_created_message(bot, group_id, topic_id, user, ticket_id)
        ticket = await database.get_ticket(ticket_id)

    await relay_user_message_to_support(bot, group_id, ticket.topic_id, message)
    await database.update_ticket_activity(ticket.id)
    await schedule_auto_close(ticket.id, context)


async def create_ticket_topic(bot, group_id: int, user) -> int:
    try:
        topic = await bot.create_forum_topic(group_id, name=user.full_name[:35])
        return topic.message_thread_id
    except BadRequest as exc:
        LOGGER.error("Failed to create forum topic: %s", exc)
        raise


async def send_ticket_created_message(bot, group_id: int, topic_id: int, user, ticket_id: int) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🤝 Взять тикет", callback_data=f"{TAKE_TICKET_CALLBACK}:{ticket_id}")]]
    )
    intro = (
        f"Новый вопрос от {format_user_mention(user)}\n"
        f"ID пользователя: <code>{user.id}</code>\n"
        "Нажмите кнопку ниже, чтобы взять обращение."
    )
    await bot.send_message(
        group_id,
        intro,
        message_thread_id=topic_id,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def relay_user_message_to_support(bot, group_id: int, topic_id: int, message: Message) -> None:
    try:
        await bot.copy_message(group_id, message.chat_id, message.message_id, message_thread_id=topic_id)
    except BadRequest:
        text = message.text or message.caption
        if text:
            await bot.send_message(
                group_id,
                f"Сообщение от пользователя: {text}",
                message_thread_id=topic_id,
            )


async def relay_support_message_to_user(
    bot,
    ticket: database.Ticket,
    message: Message,
) -> None:
    try:
        await bot.copy_message(ticket.user_id, message.chat_id, message.message_id)
    except Forbidden:
        LOGGER.warning("Failed to deliver message to user %s", ticket.user_id)


async def take_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    user = query.from_user
    if not user:
        return

    parts = query.data.split(":")
    if len(parts) != 2:
        return
    _, ticket_id_str = parts
    ticket = await database.get_ticket(int(ticket_id_str))
    if not ticket or ticket.status == "closed":
        await query.edit_message_text("Тикет уже закрыт.")
        return

    await ensure_support_profile(user)
    support = await database.get_support_member(user.id)
    if not support or support.status != "active":
        await query.edit_message_text("У вас нет доступа к панели поддержки.")
        return

    if ticket.assigned_support_id and ticket.assigned_support_id != user.id:
        await query.answer("Тикет уже взят другим сотрудником.", show_alert=True)
        return

    await database.assign_ticket(ticket.id, user.id)
    await query.edit_message_text(
        f"Тикет взял {format_user_mention(user)}. Используйте /panel для управления обращением."
    )
    await context.bot.send_message(
        ticket.user_id,
        f"Вашим вопросом займётся {format_user_mention(user)}. Пожалуйста, ожидайте ответа.",
    )


async def support_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in {ChatType.SUPERGROUP, ChatType.GROUP}:
        return
    if message.message_thread_id is None:
        return

    ticket = await database.get_ticket_by_thread(message.message_thread_id, chat.id)
    if not ticket:
        return

    if message.text and message.text.startswith("/panel"):
        return  # handled by command handler

    support_member = await database.get_support_member(user.id)
    is_owner_flag = await is_owner(user)
    if support_member and support_member.status != "active" and not is_owner_flag:
        await context.bot.send_message(
            chat.id,
            "Ваш статус не позволяет отвечать в этом тикете.",
            reply_to_message_id=message.message_id,
            message_thread_id=message.message_thread_id,
        )
        try:
            await message.delete()
        except BadRequest:
            LOGGER.warning("Failed to delete message from suspended staff %s", user.id)
        return
    if ticket.assigned_support_id and ticket.assigned_support_id != user.id and not is_owner_flag:
        if support_member and support_member.is_admin:
            pass
        else:
            await context.bot.send_message(
                chat.id,
                "Этот тикет взял другой сотрудник.",
                reply_to_message_id=message.message_id,
                message_thread_id=message.message_thread_id,
            )
            try:
                await message.delete()
            except BadRequest:
                LOGGER.warning("Failed to delete message in ticket %s", ticket.id)
            return

    if support_member is None:
        await ensure_support_profile(user)

    await relay_support_message_to_user(context.bot, ticket, message)
    await database.update_ticket_activity(ticket.id)
    await schedule_auto_close(ticket.id, context)


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in {ChatType.SUPERGROUP, ChatType.GROUP} or message.message_thread_id is None:
        await message.reply_text("Панель доступна только внутри темы обращения.")
        return

    ticket = await database.get_ticket_by_thread(message.message_thread_id, chat.id)
    if not ticket:
        await message.reply_text("Тикет не найден.")
        return

    support = await database.get_support_member(user.id)
    owner = await is_owner(user)
    if not owner and (not support or support.status != "active"):
        await message.reply_text("У вас нет доступа к панели поддержки.")
        return

    if not owner and ticket.assigned_support_id not in (None, user.id):
        await message.reply_text("Этот тикет закреплён за другим сотрудником.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Закрыть обращение", callback_data=f"{PANEL_CALLBACK}:close:{ticket.id}"),
                InlineKeyboardButton("📢 Позвать старшего", callback_data=f"{PANEL_CALLBACK}:senior:{ticket.id}"),
            ],
            [
                InlineKeyboardButton("⛔ Забанить пользователя", callback_data=f"{PANEL_CALLBACK}:ban:{ticket.id}"),
            ],
        ]
    )
    await message.reply_text("Панель управления тикетом:", reply_markup=keyboard)


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    data = query.data.split(":")
    if len(data) != 3:
        return
    _, action, ticket_id_str = data
    ticket = await database.get_ticket(int(ticket_id_str))
    if not ticket:
        await query.edit_message_text("Тикет не найден.")
        return

    user = query.from_user
    support = await database.get_support_member(user.id)
    owner = await is_owner(user)
    if not owner and (not support or support.status != "active"):
        await query.edit_message_text("У вас нет доступа к панели поддержки.")
        return

    if not owner and ticket.assigned_support_id not in (None, user.id):
        await query.edit_message_text("Обращение закреплено за другим сотрудником.")
        return

    if action == "close":
        await close_ticket_flow(ticket, support, context, notify_user=True)
        await query.edit_message_text("Обращение закрыто. Пользователь уведомлён.")
    elif action == "senior":
        await escalate_ticket(ticket, user, context)
        await query.edit_message_text("Старший сотрудник уведомлён.")
    elif action == "ban":
        await ban_user_from_ticket(ticket, context)
        await query.edit_message_text("Пользователь заблокирован и уведомлён.")


async def close_ticket_flow(
    ticket: database.Ticket,
    support: Optional[database.SupportMember],
    context: ContextTypes.DEFAULT_TYPE,
    notify_user: bool,
) -> None:
    await database.close_ticket(ticket.id)
    await unschedule_auto_close(ticket.id, context)
    if support:
        wage_setting = await database.get_setting("default_ticket_wage")
        wage_default = int(wage_setting) if wage_setting else CONFIG.default_ticket_wage
        wage = support.wage_per_ticket or wage_default
        await database.adjust_support_balance(support.user_id, wage)
        await database.increment_ticket_counter(support.user_id)
        await database.add_wage_record(support.user_id, ticket.id, wage)
    if notify_user:
        await send_rating_request(ticket, context)
    try:
        await context.bot.close_forum_topic(ticket.group_id, ticket.topic_id)
        await context.bot.delete_forum_topic(ticket.group_id, ticket.topic_id)
    except BadRequest:
        LOGGER.warning("Failed to delete topic for ticket %s", ticket.id)


async def escalate_ticket(ticket: database.Ticket, requester, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_username = CONFIG.owner_username
    owner_id_str = await database.get_setting("owner_id")
    text = (
        f"{format_user_mention(requester)} позвал старшего на тикет #{ticket.id}.\n"
        f"Пользователь: <code>{ticket.user_full_name}</code> ({ticket.user_id})"
    )
    await context.bot.send_message(
        ticket.group_id,
        f"Старший вызван {format_user_mention(requester)}",
        message_thread_id=ticket.topic_id,
    )
    if owner_id_str:
        owner_id = int(owner_id_str)
        await context.bot.send_message(owner_id, text, parse_mode="HTML")
        await database.assign_ticket(ticket.id, owner_id)
        await context.bot.send_message(
            ticket.user_id,
            f"Ваш вопрос передан старшему специалисту @{owner_username}.",
        )
    else:
        await context.bot.send_message(ticket.group_id, f"Уведомите @{owner_username}", message_thread_id=ticket.topic_id)
        if requester:
            await database.assign_ticket(ticket.id, requester.id)


async def ban_user_from_ticket(ticket: database.Ticket, context: ContextTypes.DEFAULT_TYPE) -> None:
    await database.add_ban(ticket.user_id, reason=f"Тикет #{ticket.id}")
    await context.bot.send_message(
        ticket.user_id,
        "Ваш доступ к поддержке ограничен. Обратитесь к администрации, если считаете это ошибкой.",
    )
    await close_ticket_flow(ticket, None, context, notify_user=False)


async def send_rating_request(ticket: database.Ticket, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        ticket.user_id,
        "Ваше обращение закрыто. Пожалуйста, оцените работу специалиста:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(str(i), callback_data=f"{RATING_CALLBACK}:{ticket.id}:{i}") for i in range(1, 6)]
            ]
        ),
    )


async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    _, ticket_id_str, rating_str = query.data.split(":")
    ticket = await database.get_ticket(int(ticket_id_str))
    if not ticket:
        await query.edit_message_text("Обращение не найдено.")
        return
    await database.set_ticket_rating(ticket.id, int(rating_str))
    await query.edit_message_text("Спасибо за вашу оценку!")


async def reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction = update.message_reaction
    if not reaction:
        return
    message = reaction.message
    if not message or message.message_thread_id is None:
        return
    chat = message.chat
    ticket = await database.get_ticket_by_thread(message.message_thread_id, chat.id)
    if not ticket:
        return
    emoji_list = [r.emoji for r in reaction.new_reaction if isinstance(r, ReactionTypeEmoji)]
    if not emoji_list:
        return
    text = "Поддержка отреагировала: " + ", ".join(emoji_list)
    await context.bot.send_message(ticket.user_id, text)


async def schedule_auto_close(ticket_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    job_queue = context.job_queue
    if job_queue is None:
        return
    await unschedule_auto_close(ticket_id, context)
    job_queue.run_once(
        auto_close_job,
        AUTO_CLOSE_TIMEOUT.total_seconds(),
        data={"ticket_id": ticket_id},
        name=f"auto_close_{ticket_id}",
    )


async def unschedule_auto_close(ticket_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    job_queue = context.job_queue
    if job_queue is None:
        return
    jobs = job_queue.get_jobs_by_name(f"auto_close_{ticket_id}")
    for job in jobs:
        job.schedule_removal()


async def auto_close_job(context: CallbackContext) -> None:
    data = context.job.data or {}
    ticket_id = data.get("ticket_id")
    if not ticket_id:
        return
    ticket = await database.get_ticket(ticket_id)
    if not ticket or ticket.status == "closed":
        return
    support = None
    if ticket.assigned_support_id:
        support = await database.get_support_member(ticket.assigned_support_id)
    await close_ticket_flow(ticket, support, context, notify_user=True)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return

    await database.set_setting("owner_id", str(user.id))
    await ensure_support_profile(user, make_admin=True)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Добавить сотрудника", callback_data=f"{ADMIN_CALLBACK}:add")],
            [InlineKeyboardButton("Список сотрудников", callback_data=f"{ADMIN_CALLBACK}:list")],
            [InlineKeyboardButton("Настроить оплату", callback_data=f"{ADMIN_CALLBACK}:wage")],
            [InlineKeyboardButton("Показать статистику", callback_data=f"{ADMIN_CALLBACK}:stats")],
        ]
    )
    await update.effective_message.reply_text("Панель администратора:", reply_markup=keyboard)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = query.from_user
    if not user or not await is_owner(user):
        await query.answer("Недостаточно прав", show_alert=True)
        return
    await query.answer()
    _, action = query.data.split(":", 1)
    if action == "list":
        members = await database.list_support_members()
        if not members:
            await query.edit_message_text("Список сотрудников пуст.")
            return
        lines = [
            f"<b>{m.full_name}</b> ({m.username or m.user_id}) — {m.status}, баланс: {m.balance}₽" for m in members
        ]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    elif action == "wage":
        wage = await database.get_setting("default_ticket_wage") or str(CONFIG.default_ticket_wage)
        await query.edit_message_text(
            f"Текущая оплата за тикет: {wage}₽.\nОтправьте команду /setwage <сумма> в этом чате, чтобы изменить."
        )
    elif action == "stats":
        total = await database.get_total_resolved_tickets()
        await query.edit_message_text(f"Всего закрытых тикетов: {total}")
    elif action == "add":
        await query.edit_message_text(
            "Отправьте команду /addsupport <user_id> <Имя> <Фамилия> в этом чате, чтобы добавить сотрудника."
        )


async def add_support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) < 1:
        await update.effective_message.reply_text(
            "Использование: /addsupport <user_id> [username] [Имя Фамилия]"
        )
        return
    try:
        support_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return
    username = None
    full_name = "Сотрудник"
    if len(context.args) >= 2:
        username = context.args[1].lstrip("@")
    if len(context.args) >= 3:
        full_name = " ".join(context.args[2:])
    await database.add_support_member(
        database.SupportMember(
            user_id=support_id,
            username=username,
            full_name=full_name,
            status="active",
            tickets_resolved=0,
            balance=0,
            total_penalties=0,
            wallet=None,
            is_admin=False,
            wage_per_ticket=int(await database.get_setting("default_ticket_wage") or CONFIG.default_ticket_wage),
        )
    )
    await update.effective_message.reply_text("Сотрудник добавлен и может воспользоваться /me.")


async def setwage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /setwage <сумма>")
        return
    try:
        wage = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Сумма должна быть числом.")
        return
    await database.set_setting("default_ticket_wage", str(wage))
    await update.effective_message.reply_text(f"Новая ставка за тикет: {wage}₽")


async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    support = await database.get_support_member(user.id)
    if not support:
        await update.effective_message.reply_text("Вы не зарегистрированы как сотрудник поддержки.")
        return
    wage_history = await database.list_wage_history(user.id)
    history_lines = [
        f"#{item['ticket_id']} — {item['amount']}₽ ({item['created_at']})" for item in wage_history
    ]
    stats_text = (
        f"<b>{support.full_name}</b>\n"
        f"Статус: {support.status}\n"
        f"Решено тикетов: {support.tickets_resolved}\n"
        f"Баланс к выплате: {support.balance}₽\n"
        f"Всего штрафов: {support.total_penalties}₽\n"
        f"Оплата за тикет: {support.wage_per_ticket or await database.get_setting('default_ticket_wage')}₽\n"
        f"Реквизиты: {support.wallet or 'не указаны'}\n\n"
        "Последние начисления:\n" + ("\n".join(history_lines) if history_lines else "Нет данных")
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Изменить реквизиты", callback_data=f"me:update_wallet:{user.id}")]]
    )
    await update.effective_message.reply_text(stats_text, parse_mode="HTML", reply_markup=keyboard)


async def me_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = query.from_user
    support = await database.get_support_member(user.id)
    if not support:
        await query.answer("Вы не в списке поддержки", show_alert=True)
        return
    await query.answer()
    _, action, _ = query.data.split(":")
    if action == "update_wallet":
        context.user_data["awaiting_wallet"] = True
        await query.edit_message_text("Отправьте новые реквизиты одним сообщением.")


async def collect_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_wallet"):
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    wallet_text = message.text or message.caption
    if not wallet_text:
        await message.reply_text("Пожалуйста, отправьте текстовые реквизиты.")
        return
    await database.update_support_wallet(user.id, wallet_text)
    context.user_data.pop("awaiting_wallet", None)
    await message.reply_text("Реквизиты сохранены.")


async def penalty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Использование: /penalty <user_id> <сумма>")
        return
    try:
        support_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("ID и сумма должны быть числами.")
        return
    await database.add_penalty(support_id, amount)
    await update.effective_message.reply_text("Штраф применён.")


async def suspend_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Использование: /suspend <user_id> <status> (active/suspended/fired)")
        return
    support_id = int(context.args[0])
    status = context.args[1]
    await database.update_support_status(support_id, status)
    await update.effective_message.reply_text("Статус сотрудника обновлён.")


async def set_support_wage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not await is_owner(user):
        await update.effective_message.reply_text("Команда доступна только владельцу бота.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Использование: /setwagefor <user_id> <сумма>")
        return
    support_id = int(context.args[0])
    wage = int(context.args[1])
    await database.set_support_wage(support_id, wage)
    await update.effective_message.reply_text("Ставка сотрудника обновлена.")


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запустить бота"),
            BotCommand("panel", "Панель управления тикетом"),
            BotCommand("me", "Моя статистика"),
        ]
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await database.init_db(CONFIG.default_ticket_wage)
    application = (
        ApplicationBuilder()
        .token(CONFIG.token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link", link_group))
    application.add_handler(CommandHandler("panel", panel_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("addsupport", add_support_command))
    application.add_handler(CommandHandler("setwage", setwage_command))
    application.add_handler(CommandHandler("penalty", penalty_command))
    application.add_handler(CommandHandler("suspend", suspend_command))
    application.add_handler(CommandHandler("setwagefor", set_support_wage_command))
    application.add_handler(CommandHandler("me", me_command))

    application.add_handler(CallbackQueryHandler(take_ticket, pattern=f"^{TAKE_TICKET_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(panel_callback, pattern=f"^{PANEL_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(rating_callback, pattern=f"^{RATING_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=f"^{ADMIN_CALLBACK}"))
    application.add_handler(CallbackQueryHandler(me_callback, pattern=r"^me:"))

    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), handle_private_message))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, collect_wallet))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS, support_message_handler))

    application.add_handler(MessageReactionHandler(reaction_handler))

    application.post_init = set_bot_commands

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await application.updater.wait()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
