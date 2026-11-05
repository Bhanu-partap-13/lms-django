import secrets
import uuid
from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_invite_code():
    """Generates a secure, URL-safe 10-character invite token."""
    return secrets.token_urlsafe(8)[:10]


class ChatRoom(models.Model):
    class RoomType(models.TextChoices):
        DIRECT = "DIRECT", "Direct (1-to-1)"
        GROUP = "GROUP", "Group"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room_type = models.CharField(
        max_length=10,
        choices=RoomType.choices,
        default=RoomType.DIRECT,
        db_index=True,
    )
    name = models.CharField(max_length=120, blank=True, null=True)
    icon = models.ImageField(upload_to="room_icons/%Y/%m/", blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_chat_rooms",
    )

    # Secure Invite Token Engine
    invite_code = models.CharField(
        max_length=16,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        default=generate_invite_code,
    )
    invite_code_expires_at = models.DateTimeField(null=True, blank=True)
    max_members = models.PositiveIntegerField(default=500)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["room_type", "created_at"]),
            models.Index(fields=["invite_code"]),
        ]

    def __str__(self):
        return self.name or f"Room-{str(self.id)[:8]}"

    def is_invite_valid(self) -> bool:
        """Validates if invite code is active, not expired, and room is within member capacity."""
        if not self.invite_code:
            return False
        if self.invite_code_expires_at and timezone.now() > self.invite_code_expires_at:
            return False
        if self.memberships.filter(is_active=True).count() >= self.max_members:
            return False
        return True


class RoomMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        MEMBER = "MEMBER", "Member"

    id = models.BigAutoField(primary_key=True)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_memberships",
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True, db_index=True)

    # Cursor for offline unread count sync
    last_read_message_id = models.UUIDField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["room", "user"], name="unique_room_membership")
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["room", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user} in {self.room} ({self.role})"


class Message(models.Model):
    class AttachmentType(models.TextChoices):
        NONE = "NONE", "None"
        IMAGE = "IMAGE", "Image"
        VOICE = "VOICE", "Voice Note"
        FILE = "FILE", "Document / Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_chat_messages",
    )

    # Content payload
    text = models.TextField(blank=True, default="")
    attachment_type = models.CharField(
        max_length=10,
        choices=AttachmentType.choices,
        default=AttachmentType.NONE,
    )
    attachment_file = models.FileField(upload_to="chat_attachments/%Y/%m/%d/", null=True, blank=True)
    attachment_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text="Duration for audio, dimensions for images, file size, etc.",
    )

    # Threading & Soft Delete
    parent_message = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # High performance composite index for reverse cursor pagination:
            models.Index(fields=["room", "-created_at", "id"]),
            models.Index(fields=["sender", "created_at"]),
        ]

    def __str__(self):
        return f"Msg {str(self.id)[:8]} by {self.sender} in {self.room}"


class MessageReaction(models.Model):
    id = models.BigAutoField(primary_key=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_reactions",
    )
    emoji = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["message", "user", "emoji"], name="unique_user_reaction_per_emoji")
        ]
        indexes = [
            models.Index(fields=["message", "emoji"]),
        ]

    def __str__(self):
        return f"{self.user} reacted {self.emoji} on {str(self.message.id)[:8]}"


class ReadReceipt(models.Model):
    class Status(models.TextChoices):
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"

    id = models.BigAutoField(primary_key=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="read_receipts")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_receipts",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DELIVERED)
    timestamp = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["message", "user", "status"], name="unique_message_user_status")
        ]
        indexes = [
            models.Index(fields=["message", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self):
        return f"{self.user} marked {str(self.message.id)[:8]} as {self.status}"

