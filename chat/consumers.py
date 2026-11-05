import json
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import ChatRoom, RoomMember, Message, ReadReceipt, MessageReaction


class ChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get("user")
        self.room_id = self.scope["url_route"]["kwargs"].get("room_id")
        self.room_group_name = f"room_{self.room_id}"

        # 1. Verify User Authentication
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        # 2. Verify Room Membership
        is_member = await self.check_membership(self.user.id, self.room_id)
        if not is_member:
            await self.close(code=4003)
            return

        # 3. Join Channel Layer Group
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive_json(self, content):
        action = content.get("action")

        if action == "send_message":
            text = content.get("text", "").strip()
            temp_id = content.get("temp_id")
            attachment_type = content.get("attachment_type", Message.AttachmentType.NONE)
            parent_id = content.get("parent_id")

            if not text and attachment_type == Message.AttachmentType.NONE:
                return

            # Persist message to database
            msg_data = await self.persist_message(
                user_id=self.user.id,
                room_id=self.room_id,
                text=text,
                attachment_type=attachment_type,
                parent_id=parent_id,
            )

            # 1. Immediate ACK to Sender (Single-Tick "SENT")
            await self.send_json({
                "type": "ack",
                "temp_id": temp_id,
                "message_id": str(msg_data["id"]),
                "created_at": msg_data["created_at"],
                "status": "SENT",
            })

            # 2. Broadcast message to all active room participants
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message_broadcast",
                    "sender_id": str(self.user.id),
                    "sender_name": getattr(self.user, "get_full_name", lambda: "")() or self.user.get_username(),
                    "message_id": str(msg_data["id"]),
                    "text": text,
                    "attachment_type": attachment_type,
                    "parent_id": parent_id,
                    "created_at": msg_data["created_at"],
                },
            )

        elif action in ("mark_delivered", "mark_read"):
            message_id = content.get("message_id")
            if not message_id:
                return

            receipt_status = (
                ReadReceipt.Status.READ
                if action == "mark_read"
                else ReadReceipt.Status.DELIVERED
            )

            await self.record_receipt(self.user.id, self.room_id, message_id, receipt_status)

            # Broadcast status update (grey or blue double-tick)
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "receipt_broadcast",
                    "user_id": str(self.user.id),
                    "message_id": str(message_id),
                    "status": receipt_status,
                },
            )

        elif action == "add_reaction":
            message_id = content.get("message_id")
            emoji = content.get("emoji")
            if message_id and emoji:
                await self.record_reaction(self.user.id, message_id, emoji)
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        "type": "reaction_broadcast",
                        "user_id": str(self.user.id),
                        "message_id": str(message_id),
                        "emoji": emoji,
                    },
                )

        elif action == "sync_missed":
            last_message_id = content.get("last_message_id")
            missed = await self.get_missed_messages(self.room_id, last_message_id)
            await self.send_json({
                "type": "sync_missed_response",
                "messages": missed,
            })

    # =========================================================================
    # Channel Layer Group Event Handlers
    # =========================================================================
    async def chat_message_broadcast(self, event):
        await self.send_json({
            "type": "new_message",
            "message_id": event["message_id"],
            "sender_id": event["sender_id"],
            "sender_name": event["sender_name"],
            "text": event["text"],
            "attachment_type": event["attachment_type"],
            "parent_id": event["parent_id"],
            "created_at": event["created_at"],
        })

    async def receipt_broadcast(self, event):
        await self.send_json({
            "type": "receipt_update",
            "user_id": event["user_id"],
            "message_id": event["message_id"],
            "status": event["status"],
        })

    async def reaction_broadcast(self, event):
        await self.send_json({
            "type": "reaction_update",
            "user_id": event["user_id"],
            "message_id": event["message_id"],
            "emoji": event["emoji"],
        })

    # =========================================================================
    # Async Database Helpers
    # =========================================================================
    @database_sync_to_async
    def check_membership(self, user_id, room_id):
        return RoomMember.objects.filter(
            user_id=user_id,
            room_id=room_id,
            is_active=True,
        ).exists()

    @database_sync_to_async
    def persist_message(self, user_id, room_id, text, attachment_type, parent_id=None):
        parent = None
        if parent_id:
            parent = Message.objects.filter(id=parent_id, room_id=room_id).first()

        msg = Message.objects.create(
            sender_id=user_id,
            room_id=room_id,
            text=text,
            attachment_type=attachment_type,
            parent_message=parent,
        )
        return {
            "id": msg.id,
            "created_at": msg.created_at.isoformat(),
        }

    @database_sync_to_async
    def record_receipt(self, user_id, room_id, message_id, status):
        ReadReceipt.objects.update_or_create(
            user_id=user_id,
            message_id=message_id,
            defaults={"status": status, "timestamp": timezone.now()},
        )
        if status == ReadReceipt.Status.READ:
            RoomMember.objects.filter(
                user_id=user_id,
                room_id=room_id,
            ).update(last_read_message_id=message_id)

    @database_sync_to_async
    def record_reaction(self, user_id, message_id, emoji):
        MessageReaction.objects.update_or_create(
            user_id=user_id,
            message_id=message_id,
            defaults={"emoji": emoji},
        )

    @database_sync_to_async
    def get_missed_messages(self, room_id, last_message_id):
        qs = Message.objects.filter(room_id=room_id, is_deleted=False)
        if last_message_id:
            anchor = Message.objects.filter(id=last_message_id, room_id=room_id).first()
            if anchor:
                qs = qs.filter(created_at__gt=anchor.created_at)
        messages = qs.order_by("created_at")[:100]
        return [
            {
                "id": str(m.id),
                "sender_id": str(m.sender_id),
                "text": m.text,
                "attachment_type": m.attachment_type,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ]

