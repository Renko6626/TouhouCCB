"""Integration settings reuse THCCB's existing administrator authorization."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.users import current_superuser
from app.models.base import User
from app.schemas.wavelength import IntegrationConfigRead, IntegrationConfigUpdate, RoundKey, RoundSummary
from app.services import wavelength, wavelength_config

class PrivateConfigRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default errors include the submitted input, including keys.
                raise HTTPException(422, detail=[
                    {key: error[key] for key in ('loc', 'msg', 'type')}
                    for error in exc.errors()
                ]) from None
        return validated


router = APIRouter(route_class=PrivateConfigRoute)


@router.get('/config', response_model=IntegrationConfigRead)
async def get_config(admin: User = Depends(current_superuser), db: AsyncSession = Depends(get_async_session)):
    return await wavelength_config.public_config(db)


@router.put('/config', response_model=IntegrationConfigRead)
async def update_config(req: IntegrationConfigUpdate, admin: User = Depends(current_superuser),
                        db: AsyncSession = Depends(get_async_session)):
    return await wavelength_config.save_config(db, req, admin.id)


@router.post('/rounds/{round_key}/cancel', response_model=RoundSummary)
async def cancel_round(round_key: RoundKey, admin: User = Depends(current_superuser),
                       db: AsyncSession = Depends(get_async_session)):
    return await wavelength.finish_round(db, round_key, cancel=True, operator_user_id=admin.id)
