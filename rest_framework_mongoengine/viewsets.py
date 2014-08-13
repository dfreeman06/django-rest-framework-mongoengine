from __future__ import unicode_literals

from functools import update_wrapper
from django.utils.decorators import classonlymethod
from rest_framework import views, generics, mixins
from rest_framework.viewsets import ViewSetMixin
from rest_framework_mongoengine.generics import MongoAPIView


class ViewSet(ViewSetMixin, MongoAPIView):
    """
    The base ViewSet class does not provide any actions by default.
    """
    pass


class GenericViewSet(ViewSetMixin, MongoAPIView):
    """
    The GenericViewSet class does not provide any actions by default,
    but does include the base set of generic view behavior, such as
    the `get_object` and `get_queryset` methods.
    """
    pass


class ReadOnlyModelViewSet(mixins.RetrieveModelMixin,
                           mixins.ListModelMixin,
                           GenericViewSet):
    """
    A viewset that provides default `list()` and `retrieve()` actions.
    """
    pass


class ModelViewSet(mixins.CreateModelMixin,
                    mixins.RetrieveModelMixin,
                    mixins.UpdateModelMixin,
                    mixins.DestroyModelMixin,
                    mixins.ListModelMixin,
                    GenericViewSet):
    """
    A viewset that provides default `create()`, `retrieve()`, `update()`,
    `partial_update()`, `destroy()` and `list()` actions.
    """
    pass