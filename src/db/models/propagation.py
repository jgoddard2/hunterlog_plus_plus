from datetime import datetime
import logging as L
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from marshmallow_sqlalchemy import SQLAlchemyAutoSchema

Base = declarative_base()
engine = sa.create_engine("sqlite:///propagation.db")

log = L.getLogger(__name__)


class PropagationReport(Base):
    """
    Stores propagation data from PSKReporter, WSPRnet, or other sources.
    Used to calculate real-time propagation estimates for SOTA/POTA spots.
    """
    __tablename__ = "propagation_reports"
    
    id = sa.Column(sa.Integer, primary_key=True)
    timestamp = sa.Column(sa.DateTime, nullable=False, index=True)
    
    # Transmitter information
    tx_call = sa.Column(sa.String(20), nullable=False)
    tx_grid = sa.Column(sa.String(6), nullable=False, index=True)
    tx_lat = sa.Column(sa.Float, nullable=True)
    tx_lon = sa.Column(sa.Float, nullable=True)
    
    # Receiver information
    rx_call = sa.Column(sa.String(20), nullable=False)
    rx_grid = sa.Column(sa.String(6), nullable=False, index=True)
    rx_lat = sa.Column(sa.Float, nullable=True)
    rx_lon = sa.Column(sa.Float, nullable=True)
    
    # Propagation data
    frequency = sa.Column(sa.Float, nullable=False)  # in kHz
    band = sa.Column(sa.String(10), nullable=True, index=True)  # e.g., "20m", "40m"
    mode = sa.Column(sa.String(10), nullable=False)  # e.g., "FT8", "WSPR", "CW"
    snr = sa.Column(sa.Float, nullable=False)  # Signal-to-Noise Ratio in dB
    
    # Source information
    source = sa.Column(sa.String(20), nullable=False)  # e.g., "pskreporter", "wspr"
    
    # Distance (calculated)
    distance_km = sa.Column(sa.Float, nullable=True)
    
    def __repr__(self):
        return f"<PropagationReport(tx={self.tx_call}, rx={self.rx_call}, snr={self.snr}dB, band={self.band})>"


class PropagationReportSchema(SQLAlchemyAutoSchema):
    class Meta:
        model = PropagationReport
        load_instance = True


Base.metadata.create_all(engine)
