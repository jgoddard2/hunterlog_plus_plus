import logging as L
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from sqlalchemy.orm import scoped_session
from sqlalchemy import func, and_
from db.models.propagation import PropagationReport, PropagationReportSchema

logging = L.getLogger(__name__)


class PropagationQuery:
    '''Internal DB queries against the PropagationReport table.'''

    def __init__(self, session: scoped_session):
        self.session = session

    def store_propagation_reports(self, reports: List[dict]) -> int:
        """
        Bulk insert propagation reports.
        
        Args:
            reports: List of dicts containing propagation data
            
        Returns:
            Number of reports inserted
        """
        try:
            schema = PropagationReportSchema()
            report_objects = schema.load(reports, session=self.session, many=True)
            self.session.add_all(report_objects)
            self.session.commit()
            logging.info(f"Stored {len(reports)} propagation reports")
            return len(reports)
        except Exception as ex:
            logging.error(f"Error storing propagation reports: {ex}", exc_info=True)
            self.session.rollback()
            return 0

    def get_average_snr(
        self, 
        user_grid: str, 
        target_grid: str, 
        band: Optional[str] = None,
        hours: int = 2
    ) -> Optional[float]:
        """
        Calculate average SNR for a path from user to target location.
        
        Args:
            user_grid: User's 4 or 6 character grid square
            target_grid: Target station's 4 or 6 character grid square
            band: Optional band filter (e.g., "20m")
            hours: Number of hours to look back (default: 2)
            
        Returns:
            Average SNR in dB, or None if no data available
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        
        # Truncate grids to 4 characters for broader matching
        user_grid_4 = user_grid[:4].upper() if user_grid else ""
        target_grid_4 = target_grid[:4].upper() if target_grid else ""
        
        query = self.session.query(func.avg(PropagationReport.snr).label('avg_snr')) \
            .filter(PropagationReport.timestamp >= cutoff_time)
        
        # Look for paths FROM target TO user (receiver = user location)
        # This tells us if they can hear us
        query = query.filter(
            and_(
                PropagationReport.tx_grid.like(f"{target_grid_4}%"),
                PropagationReport.rx_grid.like(f"{user_grid_4}%")
            )
        )
        
        if band:
            query = query.filter(PropagationReport.band == band)
        
        result = query.first()
        
        if result and result.avg_snr is not None:
            logging.debug(f"Average SNR for {target_grid} -> {user_grid} ({band}): {result.avg_snr:.1f}dB")
            return float(result.avg_snr)
        
        logging.debug(f"No propagation data found for {target_grid} -> {user_grid} ({band})")
        return None

    def get_propagation_estimate(
        self,
        user_grid: str,
        target_grid: str,
        band: Optional[str] = None,
        mode: Optional[str] = None,
        ssb_threshold: float = 10.0,
        digital_threshold: float = -15.0
    ) -> Tuple[Optional[float], str]:
        """
        Get propagation estimate and status for a path.
        
        Args:
            user_grid: User's grid square
            target_grid: Target grid square
            band: Optional band filter
            mode: Optional mode (for future filtering)
            ssb_threshold: SNR threshold for SSB (default: 10dB)
            digital_threshold: SNR threshold for digital modes (default: -15dB)
            
        Returns:
            Tuple of (avg_snr, status) where status is one of:
            'ssb', 'digital', 'not_reachable', 'no_data'
        """
        avg_snr = self.get_average_snr(user_grid, target_grid, band, hours=2)
        
        if avg_snr is None:
            return (None, 'no_data')
        
        if avg_snr >= ssb_threshold:
            return (avg_snr, 'ssb')
        elif avg_snr >= digital_threshold:
            return (avg_snr, 'digital')
        else:
            return (avg_snr, 'not_reachable')

    def clean_old_reports(self, days_old: int = 7) -> int:
        """
        Remove propagation reports older than specified days.
        
        Args:
            days_old: Number of days to keep (default: 7)
            
        Returns:
            Number of reports deleted
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days_old)
        
        try:
            deleted = self.session.query(PropagationReport) \
                .filter(PropagationReport.timestamp < cutoff_date) \
                .delete()
            self.session.commit()
            logging.info(f"Cleaned {deleted} old propagation reports (older than {days_old} days)")
            return deleted
        except Exception as ex:
            logging.error(f"Error cleaning old reports: {ex}", exc_info=True)
            self.session.rollback()
            return 0

    def get_report_count(self) -> int:
        """Get total number of propagation reports in database."""
        return self.session.query(PropagationReport).count()

    def get_latest_report_time(self) -> Optional[datetime]:
        """Get timestamp of the most recent propagation report."""
        result = self.session.query(func.max(PropagationReport.timestamp)).first()
        return result[0] if result else None

    def get_reports_by_band(self, band: str, hours: int = 1) -> List[PropagationReport]:
        """
        Get recent reports for a specific band.
        
        Args:
            band: Band name (e.g., "20m")
            hours: Hours to look back
            
        Returns:
            List of PropagationReport objects
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        
        return self.session.query(PropagationReport) \
            .filter(PropagationReport.band == band) \
            .filter(PropagationReport.timestamp >= cutoff_time) \
            .order_by(PropagationReport.timestamp.desc()) \
            .all()
