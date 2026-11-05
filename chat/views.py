import json
from datetime import timedelta
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from .models import ChatRoom, RoomMember, Message, generate_invite_code


class ChatAppView(LoginRequiredMixin, View):
    """Serve the modern, production-grade real-time chat interface."""

    def get(self, request, room_id=None):
        memberships = (
            RoomMember.objects.filter(user=request.user, is_active=True)
            .select_related("room")
            .order_by("-room__updated_at")
        )
        rooms = [m.room for m in memberships]
        active_room = None
        if room_id:
            active_room = next((r for r in rooms if str(r.id) == str(room_id)), None)
        if not active_room and rooms:
            active_room = rooms[0]

        return render(
            request,
            "chat/chat.html",
            {
                "rooms": rooms,
                "active_room": active_room,
            },
        )


class CreateRoomView(LoginRequiredMixin, View):
    """Create a new 1-to-1 or group chat room."""

    def post(self, request):
        try:
            data = json.loads(request.body.decode("utf-8")) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        room_type = data.get("room_type", ChatRoom.RoomType.GROUP)
        name = data.get("name", "").strip() or None
        max_members = int(data.get("max_members", 500))

        if room_type not in (ChatRoom.RoomType.DIRECT, ChatRoom.RoomType.GROUP):
            return JsonResponse({"error": "Invalid room_type"}, status=400)

        with transaction.atomic():
            room = ChatRoom.objects.create(
                room_type=room_type,
                name=name,
                created_by=request.user,
                max_members=max_members,
                invite_code=generate_invite_code() if room_type == ChatRoom.RoomType.GROUP else None,
            )
            # Add creator as OWNER
            RoomMember.objects.create(
                room=room,
                user=request.user,
                role=RoomMember.Role.OWNER,
                is_active=True,
            )

            # If DIRECT chat and target user_id provided, add them too
            target_user_id = data.get("target_user_id")
            if room_type == ChatRoom.RoomType.DIRECT and target_user_id:
                RoomMember.objects.create(
                    room=room,
                    user_id=target_user_id,
                    role=RoomMember.Role.MEMBER,
                    is_active=True,
                )

        return JsonResponse({
            "success": True,
            "room_id": str(room.id),
            "name": room.name,
            "room_type": room.room_type,
            "invite_code": room.invite_code,
        }, status=201)


class GenerateInviteView(LoginRequiredMixin, View):
    """Regenerate or update invite code with custom expiration."""

    def post(self, request, room_id):
        membership = (
            RoomMember.objects.select_related("room")
            .filter(
                room_id=room_id,
                user=request.user,
                is_active=True,
                role__in=[RoomMember.Role.OWNER, RoomMember.Role.ADMIN],
            )
            .first()
        )
        if not membership:
            return JsonResponse({"error": "Permission denied. Only admins can manage invite codes."}, status=403)

        try:
            data = json.loads(request.body.decode("utf-8")) if request.body else {}
        except json.JSONDecodeError:
            data = {}

        expires_in_hours = data.get("expires_in_hours")
        room = membership.room
        room.invite_code = generate_invite_code()

        if expires_in_hours:
            room.invite_code_expires_at = timezone.now() + timedelta(hours=int(expires_in_hours))
        else:
            room.invite_code_expires_at = None

        room.save(update_fields=["invite_code", "invite_code_expires_at", "updated_at"])

        invite_link = request.build_absolute_uri(f"/join/{room.invite_code}")

        return JsonResponse({
            "success": True,
            "room_id": str(room.id),
            "invite_code": room.invite_code,
            "invite_link": invite_link,
            "expires_at": room.invite_code_expires_at.isoformat() if room.invite_code_expires_at else None,
        })


class JoinRoomViaInviteView(LoginRequiredMixin, View):
    """Atomically validate invite code and join group chat."""

    def post(self, request):
        try:
            data = json.loads(request.body.decode("utf-8")) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        invite_code = data.get("invite_code", "").strip()
        if not invite_code:
            return JsonResponse({"error": "Invite code is required."}, status=400)

        with transaction.atomic():
            # Concurrency-safe: lock the room record to evaluate capacity and validity
            room = (
                ChatRoom.objects.select_for_update()
                .filter(invite_code=invite_code)
                .first()
            )

            if not room:
                return JsonResponse({"error": "Invalid invite code."}, status=404)

            if not room.is_invite_valid():
                return JsonResponse({
                    "error": "This invite link is expired or the room has reached maximum capacity."
                }, status=400)

            member, created = RoomMember.objects.get_or_create(
                room=room,
                user=request.user,
                defaults={"role": RoomMember.Role.MEMBER, "is_active": True},
            )

            if not member.is_active:
                member.is_active = True
                member.save(update_fields=["is_active"])

        return JsonResponse({
            "success": True,
            "room_id": str(room.id),
            "room_name": room.name or f"Room-{str(room.id)[:8]}",
            "already_joined": not created,
        })


class RoomMessagesListView(LoginRequiredMixin, View):
    """
    High-performance cursor pagination for chat messages.
    Query: /api/chat/rooms/<room_id>/messages/?cursor=<message_id>&limit=50
    """

    def get(self, request, room_id):
        # 1. Authorization check
        if not RoomMember.objects.filter(room_id=room_id, user=request.user, is_active=True).exists():
            return JsonResponse({"error": "Forbidden"}, status=403)

        limit = min(int(request.GET.get("limit", 50)), 100)
        cursor_id = request.GET.get("cursor")

        qs = Message.objects.filter(room_id=room_id, is_deleted=False).select_related("sender")

        # Reverse cursor pagination utilizing (room, -created_at, id) index
        if cursor_id:
            cursor_msg = Message.objects.filter(id=cursor_id, room_id=room_id).first()
            if cursor_msg:
                qs = qs.filter(created_at__lt=cursor_msg.created_at)

        messages = list(qs.order_by("-created_at")[: limit + 1])
        has_next = len(messages) > limit
        page_items = messages[:limit]

        next_cursor = str(page_items[-1].id) if (has_next and page_items) else None

        results = [
            {
                "id": str(m.id),
                "sender_id": str(m.sender_id),
                "sender_name": getattr(m.sender, "get_full_name", lambda: "")() or m.sender.get_username(),
                "text": m.text,
                "attachment_type": m.attachment_type,
                "attachment_file": m.attachment_file.url if m.attachment_file else None,
                "attachment_meta": m.attachment_meta,
                "parent_id": str(m.parent_message_id) if m.parent_message_id else None,
                "created_at": m.created_at.isoformat(),
            }
            for m in page_items
        ]

        return JsonResponse({
            "results": results,
            "next_cursor": next_cursor,
            "has_next": has_next,
        })


class UserRoomsListView(LoginRequiredMixin, View):
    """List all active rooms for the authenticated user."""

    def get(self, request):
        memberships = (
            RoomMember.objects.filter(user=request.user, is_active=True)
            .select_related("room")
            .order_by("-room__updated_at")
        )

        rooms_data = []
        for m in memberships:
            room = m.room
            last_msg = (
                Message.objects.filter(room=room, is_deleted=False)
                .order_by("-created_at")
                .first()
            )
            unread_count = 0
            if m.last_read_message_id:
                anchor = Message.objects.filter(id=m.last_read_message_id, room=room).first()
                if anchor:
                    unread_count = Message.objects.filter(
                        room=room,
                        created_at__gt=anchor.created_at,
                        is_deleted=False,
                    ).exclude(sender=request.user).count()

            rooms_data.append({
                "room_id": str(room.id),
                "name": room.name or f"Room-{str(room.id)[:8]}",
                "room_type": room.room_type,
                "role": m.role,
                "invite_code": room.invite_code,
                "last_message": {
                    "id": str(last_msg.id),
                    "text": last_msg.text,
                    "created_at": last_msg.created_at.isoformat(),
                } if last_msg else None,
                "unread_count": unread_count,
            })

        return JsonResponse({"rooms": rooms_data})

