from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path("message/", views.storefront_chat_message, name="storefront-chat-message"),
    path("track-action/", views.track_action, name="track-action"),
    path("nudges/", views.get_nudges, name="get-nudges"),
]
