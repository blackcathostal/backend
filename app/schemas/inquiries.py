from pydantic import BaseModel, EmailStr, Field


class ContactInquiryCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(default='', max_length=40)
    subject: str = Field(min_length=2, max_length=180)
    message: str = Field(max_length=5000)


class ContactInquiryOut(BaseModel):
    ok: bool = True
    message: str
