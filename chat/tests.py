import json
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from channels.testing import WebsocketCommunicator
from channels.layers import get_channel_layer
from core.asgi import application
from chat.models import ChatRoom, RoomMember, Message, ReadReceipt, MessageReaction

# Use InMemoryChannelLayer during tests — no Redis required.
TEST_CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

User = get_user_model()


class ChatModelAndInviteTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(
            email="user1@example.com",
            password="password123",
            first_name="Alice",
            last_name="Doe",
        )
        self.user2 = User.objects.create_user(
            email="user2@example.com",
            password="password123",
            first_name="Bob",
            last_name="Smith",
        )
        self.client = Client()
        self.client.force_login(self.user1)

    def test_create_group_room_generates_invite_code(self):
        room = ChatRoom.objects.create(
            name="Engineering",
            room_type=ChatRoom.RoomType.GROUP,
            created_by=self.user1,
            max_members=5,
        )
        self.assertIsNotNone(room.invite_code)
        self.assertTrue(room.is_invite_valid())

    def test_invite_code_expiry(self):
        room = ChatRoom.objects.create(
            name="Expired Room",
            room_type=ChatRoom.RoomType.GROUP,
            created_by=self.user1,
            invite_code_expires_at=timezone.now() - timedelta(minutes=10),
        )
        self.assertFalse(room.is_invite_valid())

    def test_invite_capacity_reached(self):
        room = ChatRoom.objects.create(
            name="Tiny Room",
            room_type=ChatRoom.RoomType.GROUP,
            created_by=self.user1,
            max_members=1,
        )
        RoomMember.objects.create(room=room, user=self.user1, role=RoomMember.Role.OWNER)
        # Capacity is 1 and user1 is active member, so room is full
        self.assertFalse(room.is_invite_valid())

    def test_join_room_via_invite_api(self):
        room = ChatRoom.objects.create(
            name="Joinable Room",
            room_type=ChatRoom.RoomType.GROUP,
            created_by=self.user1,
            max_members=10,
        )
        RoomMember.objects.create(room=room, user=self.user1, role=RoomMember.Role.OWNER)

        # Log in as user2
        self.client.force_login(self.user2)
        response = self.client.post(
            "/chat/join/",
            data=json.dumps({"invite_code": room.invite_code}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["already_joined"], False)
        self.assertTrue(RoomMember.objects.filter(room=room, user=self.user2, is_active=True).exists())

        # Second join should be idempotent
        response2 = self.client.post(
            "/chat/join/",
            data=json.dumps({"invite_code": room.invite_code}),
            content_type="application/json",
        )
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()["already_joined"], True)

    def test_join_with_invalid_code_fails(self):
        response = self.client.post(
            "/chat/join/",
            data=json.dumps({"invite_code": "non_existent_code"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_generate_invite_code_api(self):
        room = ChatRoom.objects.create(
            name="Admin Room",
            room_type=ChatRoom.RoomType.GROUP,
            created_by=self.user1,
        )
        RoomMember.objects.create(room=room, user=self.user1, role=RoomMember.Role.OWNER)

        old_code = room.invite_code
        response = self.client.post(
            f"/chat/rooms/{room.id}/invite/",
            data=json.dumps({"expires_in_hours": 24}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        room.refresh_from_db()
        self.assertNotEqual(room.invite_code, old_code)
        self.assertIsNotNone(room.invite_code_expires_at)


class ChatMessagesApiTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(
            email="alice@example.com",
            password="password123",
            first_name="Alice",
            last_name="Doe",
        )
        self.user2 = User.objects.create_user(
            email="bob@example.com",
            password="password123",
            first_name="Bob",
            last_name="Smith",
        )
        self.room = ChatRoom.objects.create(name="Project Chat", room_type=ChatRoom.RoomType.GROUP)
        RoomMember.objects.create(room=self.room, user=self.user1, role=RoomMember.Role.MEMBER)
        self.client = Client()
        self.client.force_login(self.user1)

    def test_cursor_pagination_messages(self):
        # Create 5 chronological messages
        messages = []
        for i in range(5):
            msg = Message.objects.create(
                room=self.room,
                sender=self.user1,
                text=f"Message {i+1}",
            )
            messages.append(msg)

        # Fetch first page with limit=2 (messages should be reverse-chronological)
        resp = self.client.get(f"/chat/rooms/{self.room.id}/messages/?limit=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["results"]), 2)
        self.assertTrue(data["has_next"])
        self.assertEqual(data["results"][0]["text"], "Message 5")
        self.assertEqual(data["results"][1]["text"], "Message 4")

        # Fetch next page using cursor
        cursor = data["next_cursor"]
        resp2 = self.client.get(f"/chat/rooms/{self.room.id}/messages/?limit=2&cursor={cursor}")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(len(data2["results"]), 2)
        self.assertEqual(data2["results"][0]["text"], "Message 3")
        self.assertEqual(data2["results"][1]["text"], "Message 2")

    def test_non_member_cannot_read_messages(self):
        self.client.force_login(self.user2)
        resp = self.client.get(f"/chat/rooms/{self.room.id}/messages/")
        self.assertEqual(resp.status_code, 403)

    def test_direct_two_way_room_creation_and_idempotency(self):
        from chat.views import get_or_create_direct_room
        # 1. Create two-way direct room
        room1, created1 = get_or_create_direct_room(self.user1, self.user2)
        self.assertTrue(created1)
        self.assertEqual(room1.room_type, ChatRoom.RoomType.DIRECT)
        self.assertEqual(room1.memberships.filter(is_active=True).count(), 2)

        # 2. Calling again in reverse order must be idempotent (no duplicate room)
        room2, created2 = get_or_create_direct_room(self.user2, self.user1)
        self.assertFalse(created2)
        self.assertEqual(room1.id, room2.id)

    def test_direct_chat_member_redirect(self):
        from library.models import Member
        member = Member.objects.create(name="Jane Reader", email="janereader@example.com")
        response = self.client.get(f"/chat/direct/member/{member.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/chat/", response.url)

    def test_chat_app_view_renders_clean_template(self):
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sarah Jenkins")
        self.assertNotContains(response, "Engineering Core")


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class ChatWebSocketConsumerTests(TestCase):
    async def test_websocket_flow(self):
        # Create users and room
        user = await User.objects.acreate(
            email="wsuser@example.com",
            password="password123",
            first_name="Charlie",
            last_name="Brown",
        )
        room = await ChatRoom.objects.acreate(name="WS Room", room_type=ChatRoom.RoomType.GROUP)
        await RoomMember.objects.acreate(room=room, user=user, role=RoomMember.Role.MEMBER)

        # Connect with authenticated user scope
        communicator = WebsocketCommunicator(
            application,
            f"/ws/chat/{room.id}/",
        )
        communicator.scope["user"] = user

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send message
        await communicator.send_json_to({
            "action": "send_message",
            "temp_id": "tmp-123",
            "text": "Hello real-time world!",
        })

        # 1. Receiver gets single-tick ACK
        ack_response = await communicator.receive_json_from()
        self.assertEqual(ack_response["type"], "ack")
        self.assertEqual(ack_response["temp_id"], "tmp-123")
        self.assertEqual(ack_response["status"], "SENT")
        msg_id = ack_response["message_id"]

        # 2. Receiver gets broadcast event
        broadcast_response = await communicator.receive_json_from()
        self.assertEqual(broadcast_response["type"], "new_message")
        self.assertEqual(broadcast_response["message_id"], msg_id)
        self.assertEqual(broadcast_response["text"], "Hello real-time world!")

        # 3. Mark read (blue double tick)
        await communicator.send_json_to({
            "action": "mark_read",
            "message_id": msg_id,
        })
        receipt_response = await communicator.receive_json_from()
        self.assertEqual(receipt_response["type"], "receipt_update")
        self.assertEqual(receipt_response["message_id"], msg_id)
        self.assertEqual(receipt_response["status"], "READ")

        await communicator.disconnect()

