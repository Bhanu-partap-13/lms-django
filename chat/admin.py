from django.contrib import admin
from .models import ChatRoom, RoomMember, Message, MessageReaction, ReadReceipt


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "room_type", "invite_code", "created_by", "created_at")
    list_filter = ("room_type", "created_at")
    search_fields = ("name", "invite_code")


@admin.register(RoomMember)
class RoomMemberAdmin(admin.ModelAdmin):
    list_display = ("room", "user", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active")
    search_fields = ("user__email", "room__name")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "room", "sender", "attachment_type", "is_deleted", "created_at")
    list_filter = ("attachment_type", "is_deleted", "created_at")
    search_fields = ("text", "sender__email")


@admin.register(MessageReaction)
class MessageReactionAdmin(admin.ModelAdmin):
    list_display = ("message", "user", "emoji", "created_at")


@admin.register(ReadReceipt)
class ReadReceiptAdmin(admin.ModelAdmin):
    list_display = ("message", "user", "status", "timestamp")
    list_filter = ("status",)

