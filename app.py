# --- Special Link Handler (ပုံ ၂ အတိုင်း Menu ပါ) ---
special_link_handler = ConversationHandler(
    entry_points=[CommandHandler('special_link', special_link_start)],
    states={
        SPECIAL_MAIN: [
            CallbackQueryHandler(special_create_callback, pattern="special_create"),
            CallbackQueryHandler(special_modify_callback, pattern="special_modify"),
            CallbackQueryHandler(special_delete_callback, pattern="special_delete"),
            CallbackQueryHandler(special_close_callback, pattern="special_close"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_COLLECT: [
            MessageHandler(filters.ALL, collect_special_messages),
            CallbackQueryHandler(create_pause_callback, pattern="create_pause"),
            CallbackQueryHandler(create_generate_callback, pattern="create_generate"),
            CallbackQueryHandler(create_cancel_callback, pattern="create_cancel")
        ],
        SPECIAL_MODIFY_SELECT: [
            CallbackQueryHandler(special_modify_select, pattern="^modify_select_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_MODIFY: [
            CallbackQueryHandler(modify_edit_callback, pattern="^modify_edit_"),
            CallbackQueryHandler(modify_whitelist_callback, pattern="^modify_whitelist_"),
            CallbackQueryHandler(modify_protect_callback, pattern="^modify_protect_"),
            CallbackQueryHandler(modify_expire_callback, pattern="^modify_expire_"),
            CallbackQueryHandler(modify_delete_callback, pattern="^modify_delete_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
        SPECIAL_EDIT_CONTENT: [
            CallbackQueryHandler(edit_add_callback, pattern="^edit_add_"),
            CallbackQueryHandler(edit_remove_callback, pattern="^edit_remove_"),
            CallbackQueryHandler(edit_back_callback, pattern="^edit_back_")
        ],
        SPECIAL_EDIT_ADD: [
            CallbackQueryHandler(edit_add_position, pattern="^add_pos_"),
            MessageHandler(filters.ALL, edit_add_collect)
        ],
        SPECIAL_EDIT_ADD_POS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_add_position_number)
        ],
        SPECIAL_EDIT_REMOVE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_remove_collect)
        ],
        SPECIAL_WHITELIST: [
            CallbackQueryHandler(whitelist_toggle, pattern="^whitelist_toggle_"),
            CallbackQueryHandler(whitelist_add, pattern="^whitelist_add_"),
            CallbackQueryHandler(whitelist_remove, pattern="^whitelist_remove_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_")
        ],
        SPECIAL_WHITELIST_ADD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, whitelist_add_collect)
        ],
        SPECIAL_WHITELIST_REMOVE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, whitelist_remove_collect)
        ],
        SPECIAL_EXPIRE: [
            CallbackQueryHandler(expire_set, pattern="^expire_set_"),
            CallbackQueryHandler(expire_remove, pattern="^expire_remove_"),
            CallbackQueryHandler(modify_back_callback, pattern="^modify_back_")
        ],
        SPECIAL_EXPIRE_SET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, expire_set_collect)
        ],
        SPECIAL_DELETE: [
            CallbackQueryHandler(special_delete_confirm, pattern="^delete_select_"),
            CallbackQueryHandler(special_delete_execute, pattern="^confirm_delete_"),
            CallbackQueryHandler(special_back_callback, pattern="special_back")
        ],
    },
    fallbacks=[CommandHandler('cancel', cancel_special)],
)

# --- Modify Link Callback (from generate) ---
application.add_handler(CallbackQueryHandler(modify_link_callback, pattern="^modify_link_"))
application.add_handler(CallbackQueryHandler(share_url_callback, pattern="^share_url_"))

# --- Add Special Link Handler ---
application.add_handler(special_link_handler)
