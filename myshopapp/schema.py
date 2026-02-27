import graphene
import chat.schema

class Query(chat.schema.Query, graphene.ObjectType):
    pass

class Mutation(chat.schema.Mutation, graphene.ObjectType):
    pass

schema = graphene.Schema(query=Query, mutation=Mutation)
