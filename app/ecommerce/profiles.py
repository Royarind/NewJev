"""
Checkout Profile
Data structure for customer information and country-specific preferences
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from enum import Enum


class Country(Enum):
    INDIA = "IN"
    USA = "US"
    UK = "GB"
    UAE = "AE"
    CANADA = "CA"
    AUSTRALIA = "AU"
    PAKISTAN = "PK"


@dataclass
class CheckoutProfile:
    # Personal (Required)
    first_name: str
    last_name: str
    email: str
    phone: str

    # Address (Required)
    address_line1: str
    city: str
    state: str
    postal_code: str

    # Optional
    address_line2: Optional[str] = ""
    country: Country = Country.USA
    payment_method: Optional[str] = None
    metadata: Dict = field(default_factory=dict)

    # Login credentials
    password: Optional[str] = None

    # Payment card details
    card_number: Optional[str] = None
    card_expiry: Optional[str] = None
    card_cvv: Optional[str] = None
    card_name: Optional[str] = None

    @property
    def card_name_resolved(self) -> str:
        return self.card_name or f"{self.first_name} {self.last_name}"

    def to_dict(self) -> Dict:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "email": self.email,
            "phone": self.phone,
            "address_line1": self.address_line1,
            "address_line2": self.address_line2,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "country": self.country.value,
            "payment_method": self.payment_method,
            "metadata": self.metadata,
            "password": self.password,
            "card_number": self.card_number,
            "card_expiry": self.card_expiry,
            "card_cvv": self.card_cvv,
            "card_name": self.card_name,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "CheckoutProfile":
        country_value = data.get("country", "US")
        if isinstance(country_value, str):
            norm = {
                "united states": "US", "usa": "US", "us": "US",
                "india": "IN", "in": "IN",
                "united kingdom": "GB", "uk": "GB", "gb": "GB",
                "united arab emirates": "AE", "uae": "AE", "ae": "AE",
                "canada": "CA", "ca": "CA",
                "australia": "AU", "au": "AU",
                "pakistan": "PK", "pk": "PK",
            }
            country_value = norm.get(country_value.lower().strip(), country_value)

        country = Country(country_value) if isinstance(country_value, str) else country_value

        return cls(
            first_name=data["first_name"],
            last_name=data["last_name"],
            email=data["email"],
            phone=data["phone"],
            address_line1=data["address_line1"],
            address_line2=data.get("address_line2", ""),
            city=data["city"],
            state=data["state"],
            postal_code=data["postal_code"],
            country=country,
            payment_method=data.get("payment_method"),
            metadata=data.get("metadata", {}),
            password=data.get("password"),
            card_number=data.get("card_number"),
            card_expiry=data.get("card_expiry"),
            card_cvv=data.get("card_cvv"),
            card_name=data.get("card_name"),
        )

    def validate_for_country(self) -> List[str]:
        errors = []
        if self.country == Country.INDIA:
            if not self.phone or len(self.phone) < 10:
                errors.append("Phone number must be at least 10 digits for India")
            if not self.postal_code or len(self.postal_code) != 6:
                errors.append("Postal code must be 6 digits for India")
        elif self.country in (Country.USA, Country.CANADA):
            if not self.postal_code or len(self.postal_code) < 5:
                errors.append(f"ZIP code must be at least 5 digits for {self.country.name}")
        elif self.country == Country.UK:
            if not self.postal_code or len(self.postal_code) < 5:
                errors.append("Postcode must be valid UK format")
        return errors
