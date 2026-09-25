from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.terms import TermsAndConditions

DEFAULT_TERMS: dict[str, dict[str, str]] = {
    "es": {
        "title": "Términos y Condiciones Generales de Servicio",
        "intro": (
            "Documento informativo de Hostal Boutique Black Cat. Al reservar o utilizar "
            "nuestras instalaciones, usted acepta íntegramente las condiciones aquí descritas."
        ),
        "body_html": """
<h2>1. Generalidades y marco legal</h2>
<p>El presente documento regula los servicios de alojamiento y prestaciones complementarias ofrecidos por Hostal Boutique Black Cat. Su aceptación es requisito para formalizar cualquier reserva o estadía.</p>
<p>Este acuerdo se rige por la legislación de la República de Chile, en particular por la Ley N.º 19.496 sobre Protección de los Derechos de los Consumidores, el Decreto Supremo N.º 194 del Ministerio de Salud (Reglamento de Hoteles y Establecimientos Similares) y las disposiciones del Servicio Nacional de Turismo (SERNATUR).</p>

<h2>2. Reservas, reservas web y pagos</h2>
<ul>
  <li><strong>Confirmación:</strong> Toda reserva efectuada por el sitio web, plataformas externas o de forma directa se considerará confirmada únicamente tras la recepción del comprobante de pago o de la garantía correspondiente.</li>
  <li><strong>Métodos de pago:</strong> Se aceptan tarjetas de débito y crédito, transferencias bancarias y efectivo en moneda nacional (CLP) o en divisa extranjera autorizada, conforme a la normativa cambiaria vigente.</li>
  <li><strong>Tarifas e impuestos:</strong> Las tarifas publicadas en la web incluyen IVA (19 %) para residentes locales. Los turistas extranjeros no residentes en Chile podrán solicitar la exención de IVA presentando pasaporte vigente y Tarjeta Única Migratoria (PDI) al momento del registro, y pagando en dólares estadounidenses (USD) o con tarjeta de crédito internacional.</li>
</ul>

<h2>3. Política de check-in, check-out y permanencia</h2>
<table>
  <thead>
    <tr><th>Concepto</th><th>Horario / condición</th><th>Detalle</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>Check-in</td>
      <td>Desde las 15:00 hrs.</td>
      <td>Es obligatorio presentar documento de identidad oficial vigente (cédula de identidad o pasaporte). El registro es individual por pasajero.</td>
    </tr>
    <tr>
      <td>Check-out</td>
      <td>Hasta las 11:00 hrs.</td>
      <td>La entrega tardía sin autorización previa podrá generar un recargo por late check-out o el cobro de una noche adicional.</td>
    </tr>
    <tr>
      <td>Early check-in / Late check-out</td>
      <td>Sujeto a disponibilidad</td>
      <td>Requiere solicitud previa y confirmación expresa de recepción. Puede aplicar un costo adicional.</td>
    </tr>
  </tbody>
</table>

<h2>4. Cancelaciones, no-show y devoluciones</h2>
<ul>
  <li><strong>Cancelación estándar:</strong> El pasajero podrá cancelar o modificar su reserva sin penalización, notificando con al menos 48 horas de anticipación a la fecha de llegada.</li>
  <li><strong>Cancelación tardía:</strong> Las cancelaciones con menos de 48 horas de anticipación facultarán al hostal a retener el valor equivalente a la primera noche de estadía.</li>
  <li><strong>No-show:</strong> Si el huésped no se presenta en la fecha programada sin aviso previo, la reserva será cancelada automáticamente y se retendrá el 100 % del depósito de garantía o del pago correspondiente a la primera noche.</li>
</ul>

<h2>5. Reglas de convivencia y uso de las instalaciones</h2>
<ul>
  <li><strong>Conducta y descanso:</strong> Hostal Boutique Black Cat promueve un ambiente de descanso y convivencia respetuosa. Se debe mantener silencio en zonas comunes y pasillos entre las 22:00 y las 08:00 hrs.</li>
  <li><strong>Ley del Tabaco:</strong> En cumplimiento de la Ley N.º 20.660, está prohibido fumar en todas las áreas cerradas del hostal, habitaciones y pasillos. Fumar en zonas no autorizadas podrá generar una multa de limpieza equivalente al valor de una noche de estadía.</li>
  <li><strong>Visitas:</strong> Por seguridad y control de acceso, no se permite el ingreso de personas no registradas a las habitaciones.</li>
  <li><strong>Mascotas:</strong> Salvo autorización previa y expresa del establecimiento, no se permite el ingreso de animales de compañía, con excepción de perros de asistencia debidamente acreditados conforme a la legislación chilena.</li>
</ul>

<h2>6. Protección de datos personales y privacidad</h2>
<p>Hostal Boutique Black Cat cumple con la Ley N.º 19.628 y su reforma mediante la Ley N.º 21.719 sobre Protección de Datos Personales. Los datos recolectados durante la reserva y el registro de pasajeros se tratan bajo criterios de confidencialidad, finalidad y proporcionalidad.</p>
<ul>
  <li><strong>Finalidad del tratamiento:</strong> Los datos personales (nombre, RUT o pasaporte, contacto y datos de pago) se utilizan exclusivamente para la gestión de la reserva, facturación, cumplimiento de registros legales ante la autoridad y comunicaciones operativas de la estadía.</li>
  <li><strong>Derechos ARSOP:</strong> El titular podrá ejercer en cualquier momento sus derechos de Acceso, Rectificación, Supresión, Oposición y Portabilidad, enviando una solicitud por escrito a los canales oficiales de contacto del hostal.</li>
  <li><strong>Videovigilancia:</strong> Por seguridad de huéspedes y personal, el establecimiento cuenta con cámaras en zonas comunes y de acceso público. Las imágenes son de uso confidencial.</li>
</ul>

<h2>7. Responsabilidades y daños al patrimonio</h2>
<ul>
  <li><strong>Bienes personales:</strong> El hostal no responde por pérdida, extravío o hurto de objetos de valor o dinero que no hayan sido entregados en custodia en recepción o resguardados en las cajas de seguridad destinadas a ese fin.</li>
  <li><strong>Daños a las instalaciones:</strong> Cualquier deterioro, rotura o daño ocasionado a la infraestructura, equipamiento o mobiliario por negligencia o dolo del huésped será cargado a su cuenta o a la tarjeta de crédito en garantía.</li>
</ul>

<h2>8. Modificaciones y jurisdicción</h2>
<p>Hostal Boutique Black Cat se reserva el derecho de actualizar estos Términos y Condiciones para adecuarlos a reformas legales o mejoras operativas. Toda controversia derivada del servicio se someterá a los tribunales ordinarios de justicia de la República de Chile.</p>
""".strip(),
    },
    "en": {
        "title": "General Terms and Conditions of Service",
        "intro": (
            "Informational document of Hostal Boutique Black Cat. By booking or using our "
            "facilities, you fully accept the conditions set out below."
        ),
        "body_html": """
<h2>1. General provisions and legal framework</h2>
<p>This document governs the accommodation services and complementary benefits offered by Hostal Boutique Black Cat. Acceptance is required to complete any booking or stay.</p>
<p>This agreement is governed by the laws of the Republic of Chile, in particular Act No. 19.496 on Consumer Protection, Ministry of Health Supreme Decree No. 194 (Hotels and Similar Establishments Regulations), and the rules of the National Tourism Service (SERNATUR).</p>

<h2>2. Bookings, online reservations and payments</h2>
<ul>
  <li><strong>Confirmation:</strong> Any booking made via the website, external platforms or directly is confirmed only after receipt of the corresponding payment proof or guarantee.</li>
  <li><strong>Payment methods:</strong> Debit and credit cards, bank transfers and cash in Chilean pesos (CLP) or authorized foreign currency are accepted, subject to applicable exchange rules.</li>
  <li><strong>Rates and taxes:</strong> Website rates include VAT (19 %) for local residents. Non-resident foreign tourists may request VAT exemption by presenting a valid passport and the Unique Migration Card (PDI) at check-in, and paying in US dollars (USD) or with an international credit card.</li>
</ul>

<h2>3. Check-in, check-out and stay policy</h2>
<table>
  <thead>
    <tr><th>Item</th><th>Schedule / condition</th><th>Details</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>Check-in</td>
      <td>From 15:00</td>
      <td>A valid official ID (national ID card or passport) is mandatory. Registration is individual per guest.</td>
    </tr>
    <tr>
      <td>Check-out</td>
      <td>Until 11:00</td>
      <td>Late departure without prior authorization may incur a late check-out fee or an extra night charge.</td>
    </tr>
    <tr>
      <td>Early check-in / Late check-out</td>
      <td>Subject to availability</td>
      <td>Requires prior request and express confirmation by reception. An additional fee may apply.</td>
    </tr>
  </tbody>
</table>

<h2>4. Cancellations, no-show and refunds</h2>
<ul>
  <li><strong>Standard cancellation:</strong> Guests may cancel or modify a booking without penalty by notifying at least 48 hours before arrival.</li>
  <li><strong>Late cancellation:</strong> Cancellations with less than 48 hours’ notice entitle the hostel to retain the equivalent of the first night.</li>
  <li><strong>No-show:</strong> If the guest fails to arrive on the scheduled date without prior notice, the booking will be cancelled automatically and 100 % of the deposit or first-night payment will be retained.</li>
</ul>

<h2>5. House rules and use of facilities</h2>
<ul>
  <li><strong>Conduct and rest:</strong> Hostal Boutique Black Cat promotes a respectful rest environment. Quiet hours apply in common areas and corridors from 22:00 to 08:00.</li>
  <li><strong>Tobacco Act:</strong> In accordance with Act No. 20.660, smoking is prohibited in all enclosed areas, rooms and corridors. Smoking in unauthorized areas may result in a cleaning fine equal to one night’s stay.</li>
  <li><strong>Visitors:</strong> For security and access control, unregistered persons are not allowed in guest rooms.</li>
  <li><strong>Pets:</strong> Unless expressly authorized in advance, companion animals are not allowed, except duly accredited assistance dogs under Chilean law.</li>
</ul>

<h2>6. Personal data protection and privacy</h2>
<p>Hostal Boutique Black Cat complies with Act No. 19.628 and its reform through Act No. 21.719 on Personal Data Protection. Data collected during booking and guest registration are processed under confidentiality, purpose limitation and proportionality.</p>
<ul>
  <li><strong>Purpose:</strong> Personal data (name, ID/passport, contact and payment details) are used solely for booking management, invoicing, legal registers required by authorities and operational stay communications.</li>
  <li><strong>Data subject rights:</strong> You may exercise access, rectification, erasure, objection and portability rights at any time by written request through the hostel’s official contact channels.</li>
  <li><strong>CCTV:</strong> For guest and staff safety, cameras operate in common and public access areas. Recordings are confidential.</li>
</ul>

<h2>7. Liability and property damage</h2>
<ul>
  <li><strong>Personal belongings:</strong> The hostel is not liable for loss, misplacement or theft of valuables or cash not left in reception custody or secured in the designated safety boxes.</li>
  <li><strong>Damage to facilities:</strong> Any deterioration, breakage or damage to infrastructure, equipment or furniture caused by guest negligence or willful misconduct will be charged to the guest’s account or guarantee card.</li>
</ul>

<h2>8. Amendments and jurisdiction</h2>
<p>Hostal Boutique Black Cat reserves the right to update these Terms and Conditions to reflect legal reforms or operational improvements. Any dispute arising from the service shall be submitted to the ordinary courts of justice of the Republic of Chile.</p>
""".strip(),
    },
    "pt": {
        "title": "Termos e Condições Gerais de Serviço",
        "intro": (
            "Documento informativo do Hostal Boutique Black Cat. Ao reservar ou utilizar "
            "nossas instalações, você aceita integralmente as condições aqui descritas."
        ),
        "body_html": """
<h2>1. Generalidades e marco legal</h2>
<p>Este documento regula os serviços de hospedagem e prestações complementares oferecidos pelo Hostal Boutique Black Cat. Sua aceitação é requisito para formalizar qualquer reserva ou estadia.</p>
<p>Este acordo rege-se pela legislação da República do Chile, em particular pela Lei n.º 19.496 sobre Proteção dos Direitos dos Consumidores, pelo Decreto Supremo n.º 194 do Ministério da Saúde (Regulamento de Hotéis e Estabelecimentos Similares) e pelas disposições do Serviço Nacional de Turismo (SERNATUR).</p>

<h2>2. Reservas, reservas online e pagamentos</h2>
<ul>
  <li><strong>Confirmação:</strong> Toda reserva feita pelo site, plataformas externas ou de forma direta será considerada confirmada apenas após o recebimento do comprovante de pagamento ou da garantia correspondente.</li>
  <li><strong>Métodos de pagamento:</strong> Aceitam-se cartões de débito e crédito, transferências bancárias e dinheiro em moeda nacional (CLP) ou divisa estrangeira autorizada, conforme a normativa cambial vigente.</li>
  <li><strong>Tarifas e impostos:</strong> As tarifas publicadas no site incluem IVA (19 %) para residentes locais. Turistas estrangeiros não residentes no Chile poderão solicitar isenção de IVA apresentando passaporte válido e Cartão Único Migratório (PDI) no registro, e pagando em dólares americanos (USD) ou com cartão de crédito internacional.</li>
</ul>

<h2>3. Política de check-in, check-out e permanência</h2>
<table>
  <thead>
    <tr><th>Conceito</th><th>Horário / condição</th><th>Detalhe</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>Check-in</td>
      <td>A partir das 15:00</td>
      <td>É obrigatório apresentar documento de identidade oficial válido (cédula ou passaporte). O registro é individual por hóspede.</td>
    </tr>
    <tr>
      <td>Check-out</td>
      <td>Até as 11:00</td>
      <td>A saída tardia sem autorização prévia poderá gerar cobrança por late check-out ou tarifa de noite adicional.</td>
    </tr>
    <tr>
      <td>Early check-in / Late check-out</td>
      <td>Sujeito à disponibilidade</td>
      <td>Requer solicitação prévia e confirmação expressa da recepção. Pode aplicar custo adicional.</td>
    </tr>
  </tbody>
</table>

<h2>4. Cancelamentos, no-show e reembolsos</h2>
<ul>
  <li><strong>Cancelamento padrão:</strong> O hóspede poderá cancelar ou modificar a reserva sem penalização, notificando com pelo menos 48 horas de antecedência à data de chegada.</li>
  <li><strong>Cancelamento tardio:</strong> Cancelamentos com menos de 48 horas de antecedência autorizam o hostal a reter o valor equivalente à primeira noite.</li>
  <li><strong>No-show:</strong> Se o hóspede não se apresentar na data programada sem aviso prévio, a reserva será cancelada automaticamente e será retido 100 % do depósito de garantia ou do pagamento da primeira noite.</li>
</ul>

<h2>5. Regras de convivência e uso das instalações</h2>
<ul>
  <li><strong>Conduta e descanso:</strong> O Hostal Boutique Black Cat promove um ambiente de descanso e convivência respeitosa. Deve-se manter silêncio nas áreas comuns e corredores entre 22:00 e 08:00.</li>
  <li><strong>Lei do Tabaco:</strong> Em cumprimento da Lei n.º 20.660, é proibido fumar em todas as áreas fechadas, quartos e corredores. Fumar em zonas não autorizadas poderá gerar multa de limpeza equivalente a uma noite de estadia.</li>
  <li><strong>Visitas:</strong> Por segurança e controle de acesso, não é permitido o ingresso de pessoas não registradas nos quartos.</li>
  <li><strong>Animais de estimação:</strong> Salvo autorização prévia e expressa, não é permitido o ingresso de animais de companhia, com exceção de cães de assistência devidamente acreditados conforme a legislação chilena.</li>
</ul>

<h2>6. Proteção de dados pessoais e privacidade</h2>
<p>O Hostal Boutique Black Cat cumpre a Lei n.º 19.628 e sua reforma pela Lei n.º 21.719 sobre Proteção de Dados Pessoais. Os dados coletados durante a reserva e o registro de hóspedes são tratados sob critérios de confidencialidade, finalidade e proporcionalidade.</p>
<ul>
  <li><strong>Finalidade:</strong> Os dados pessoais (nome, RUT/passaporte, contato e dados de pagamento) são usados exclusivamente para gestão da reserva, faturamento, cumprimento de registros legais perante a autoridade e comunicações operacionais da estadia.</li>
  <li><strong>Direitos do titular:</strong> O titular poderá exercer a qualquer momento os direitos de Acesso, Retificação, Supressão, Oposição e Portabilidade, mediante solicitação escrita pelos canais oficiais de contato do hostal.</li>
  <li><strong>Videovigilância:</strong> Por segurança de hóspedes e equipe, o estabelecimento possui câmeras em zonas comuns e de acesso público. As imagens são de uso confidencial.</li>
</ul>

<h2>7. Responsabilidades e danos ao patrimônio</h2>
<ul>
  <li><strong>Bens pessoais:</strong> O hostal não se responsabiliza por perda, extravio ou furto de objetos de valor ou dinheiro que não tenham sido entregues sob custódia na recepção ou guardados nos cofres destinados a esse fim.</li>
  <li><strong>Danos às instalações:</strong> Qualquer deterioração, quebra ou dano à infraestrutura, equipamentos ou mobiliário por negligência ou dolo do hóspede será cobrado em sua conta ou no cartão de crédito em garantia.</li>
</ul>

<h2>8. Modificações e jurisdição</h2>
<p>O Hostal Boutique Black Cat reserva-se o direito de atualizar estes Termos e Condições para adequá-los a reformas legais ou melhorias operacionais. Toda controvérsia decorrente do serviço será submetida aos tribunais ordinários de justiça da República do Chile.</p>
""".strip(),
    },
}


def serialize(row: TermsAndConditions) -> dict[str, Any]:
    return {
        "id": row.id,
        "locale": row.locale,
        "title": row.title,
        "intro": row.intro,
        "body_html": row.body_html,
        "updated_at": row.updated_at or row.created_at,
    }


def ensure_default_terms(db: Session) -> None:
    for locale, payload in DEFAULT_TERMS.items():
        row = db.query(TermsAndConditions).filter(TermsAndConditions.locale == locale).first()
        if row:
            continue
        db.add(
            TermsAndConditions(
                locale=locale,
                title=payload["title"],
                intro=payload["intro"],
                body_html=payload["body_html"],
            )
        )
    db.commit()


def get_terms(db: Session, locale: str) -> TermsAndConditions:
    ensure_default_terms(db)
    code = (locale or "es").strip().lower()[:8]
    row = db.query(TermsAndConditions).filter(TermsAndConditions.locale == code).first()
    if row:
        return row
    row = db.query(TermsAndConditions).filter(TermsAndConditions.locale == "es").first()
    if not row:
        raise LookupError("terms_missing")
    return row


def list_terms(db: Session) -> list[TermsAndConditions]:
    ensure_default_terms(db)
    return (
        db.query(TermsAndConditions)
        .order_by(TermsAndConditions.locale.asc())
        .all()
    )


def update_terms(
    db: Session,
    locale: str,
    *,
    title: str,
    intro: str,
    body_html: str,
) -> TermsAndConditions:
    ensure_default_terms(db)
    code = (locale or "es").strip().lower()[:8]
    row = db.query(TermsAndConditions).filter(TermsAndConditions.locale == code).first()
    if not row:
        row = TermsAndConditions(locale=code)
        db.add(row)
    row.title = title.strip()
    row.intro = (intro or "").strip()
    row.body_html = body_html.strip()
    db.commit()
    db.refresh(row)
    return row
