from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RolesOut(BaseModel):
    id: int
    name: str
    description: str | None = None

    model_config = {"from_attributes": True}


class UsersOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    role: RolesOut | None = None

    model_config = {"from_attributes": True}
