from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path("", views.ChatAppView.as_view(), name="chat_app"),
    path("<uuid:room_id>/", views.ChatAppView.as_view(), name="chat_app_room"),
    path("rooms/", views.UserRoomsListView.as_view(), name="user_rooms"),
    path("rooms/create/", views.CreateRoomView.as_view(), name="create_room"),
    path("rooms/<uuid:room_id>/messages/", views.RoomMessagesListView.as_view(), name="room_messages"),
    path("rooms/<uuid:room_id>/invite/", views.GenerateInviteView.as_view(), name="generate_invite"),
    path("join/", views.JoinRoomViaInviteView.as_view(), name="join_room"),
]

