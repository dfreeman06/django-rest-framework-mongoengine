__author__ = 'BryanAke@gmail.com'

from mongoengine.document import Document, EmbeddedDocument
from mongoengine import fields

class Manufacturer(Document):
    name = fields.StringField()

class Vehicle(Document):
    name = fields.StringField()
    weight = fields.IntField()

    manufacturer = fields.StringField()

class Car(Vehicle):
    manufacturer = fields.ReferenceField(Manufacturer)
    mpg = fields.IntField()

class Truck(Vehicle):
    mpg = fields.EmbeddedDocumentField(Mileage)


class Mileage(EmbeddedDocument):
    loaded = fields.IntField()
    unloaded = fields.IntField()

class FuelMileage(Mileage):
    e85 = fields.IntField()
    unleaded = fields.IntField()
