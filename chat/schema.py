import graphene
from graphene_django import DjangoObjectType
from .models import Conversation, Message
from django.contrib.auth import get_user_model

User = get_user_model()

class UserType(DjangoObjectType):
    class Meta:
        model = User
        fields = ("id", "username", "email")

class MessageType(DjangoObjectType):
    class Meta:
        model = Message
        fields = ("id", "conversation", "sender", "content", "is_ai", "timestamp")

class ConversationType(DjangoObjectType):
    class Meta:
        model = Conversation
        fields = ("id", "participants", "messages", "created_at", "updated_at")

class Query(graphene.ObjectType):
    all_conversations = graphene.List(ConversationType)
    conversation = graphene.Field(ConversationType, id=graphene.Int())
    
    def resolve_all_conversations(self, info):
        # In a real app, filter by current user
        return Conversation.objects.all()

    def resolve_conversation(self, info, id):
        try:
            return Conversation.objects.get(pk=id)
        except Conversation.DoesNotExist:
            return None

class CreateMessage(graphene.Mutation):
    class Arguments:
        conversation_id = graphene.Int(required=True)
        content = graphene.String(required=True)
        is_ai = graphene.Boolean()

    message = graphene.Field(MessageType)

    def mutate(self, info, conversation_id, content, is_ai=False):
        # Mock sender for now (first user if exists)
        sender = User.objects.first()
        conversation = Conversation.objects.get(pk=conversation_id)
        
        message = Message.objects.create(
            conversation=conversation,
            sender=sender,
            content=content,
            is_ai=is_ai
        )
        return CreateMessage(message=message)

class Mutation(graphene.ObjectType):
    create_message = CreateMessage.Field()
