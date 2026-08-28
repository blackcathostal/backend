from pydantic import BaseModel, EmailStr, Field


class ContactInquiryCreate(BaseModel):
    name: str
    email: EmailStr
    phone: str = Field(default="", pattern=r"^[0-9]*$")
    subject: str
    message: str


class ContactInquiryOut(BaseModel):
    ok: bool = True
    message: str
