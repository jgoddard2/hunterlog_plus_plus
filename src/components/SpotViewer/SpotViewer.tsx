import * as React from 'react';
import Button from '@mui/material/Button';
import { Backdrop, Badge, CircularProgress, Tooltip, styled } from '@mui/material';
import Alert from '@mui/material/Alert';
import { DataGrid, GridColDef, GridValueGetterParams, GridValueFormatterParams, GridFilterModel, GridSortModel, GridSortDirection, GridCellParams, GridRowClassNameParams, GridToolbar, GridToolbarContainer, GridToolbarDensitySelector, GridToolbarColumnsButton, GridToolbarQuickFilter, GridPaginationModel } from '@mui/x-data-grid';
import { GridEventListener } from '@mui/x-data-grid';
import LandscapeIcon from '@mui/icons-material/Landscape';
import ParkIcon from '@mui/icons-material/Park';
import Brightness3Icon from '@mui/icons-material/Brightness3';

import { useAppContext } from '../AppContext';
import { useConfigContext } from '../Config/ConfigContextProvider';

import { Qso } from '../../@types/QsoTypes';
import CallToolTip from './CallTooltip';
import { SpotRow } from '../../@types/Spots';

import './SpotViewer.scss'
import HuntedCheckbox from './HuntedCheckbox';
import FreqButton from './FreqButton';
import SpotCommentsButton from './SpotComments';
import { Park } from '../../@types/Parks';
import SpotTimeCell from './SpotTime';
import { SpotComments } from '../../@types/SpotComments';
import { getSummitInfo } from '../../pota';
import { Summit } from '../../@types/Summit';
import { checkApiResponse } from '../../util';
import HandleSpotRowClick from './HandleSpotRowClick';
import ScanButton from './ScanButton';

// https://mui.com/material-ui/react-table/


const createBaseColumns = (isDarkMode: boolean): GridColDef[] => [
    // { field: 'spotId', headerName: 'ID', width: 70 },
    {
        field: 'activator', headerName: 'Activator', width: 130,
        renderCell: (params: GridCellParams) => (
            <CallToolTip callsign={params.row.activator} op_hunts={params.row.op_hunts} />
        ),
    },
    {
        field: 'spotTime',
        headerName: 'Time',
        width: 85,
        type: 'dateTime',
        valueGetter: (params: GridValueGetterParams) => {
            return new Date(params.row.spotTime);
        },
        renderCell: (x) => {
            return (
                <SpotTimeCell dateTimeObj={x.row.spotTime} />
            );
        }
    },
    {
        field: 'frequency', headerName: 'Freq', width: 100, type: 'number',
        renderCell: (x) => {
            return (
                <FreqButton activator={x.row.activator} frequency={x.row.frequency} mode={x.row.mode} spotId={x.row.spotId} />
            );
        }
    },
    { field: 'mode', headerName: 'Mode', width: 80 },
    {
        field: 'locationDesc', headerName: 'Loc', width: 100,
        renderCell: (x) => {
            function getContent() {
                return (
                    <>{x.row.loc_hunts} / {x.row.loc_total} </>
                )
            };
            return (
                <>
                    {x.row.loc_hunts > 0 && (
                        <Badge
                            badgeContent={getContent()}
                            color="secondary"
                            anchorOrigin={{
                                vertical: 'top',
                                horizontal: 'right',
                            }}>
                            <span id="locationDesc">{x.row.locationDesc}</span>
                        </Badge>
                    )}
                    {x.row.loc_hunts == 0 && (
                        <span id="locationDesc">{x.row.locationDesc}</span>
                    )}
                </>
            )
        }
    },
    {
        field: 'reference', headerName: 'Reference', width: 400,
        renderCell: (x) => {
            return (
                <Badge
                    badgeContent={x.row.park_hunts}
                    color="secondary"
                    max={999}
                    anchorOrigin={{
                        vertical: 'top',
                        horizontal: 'left',
                    }}>
                    <span id="parkName">{x.row.reference} - {x.row.name}</span>
                </Badge>
            )
        }
    },
    {
        field: 'spotOrig', headerName: 'Spot', width: 250,
        // valueGetter: (params: GridValueGetterParams) => {
        //     return `${params.row.spotter || ''}: ${params.row.comments || ''}`;
        // },
        // do this to have a popup for all spots comments
        renderCell: (x) => {
            return (
                <SpotCommentsButton spotId={x.row.spotId} spotter={x.row.spotter} comments={x.row.comments} />
            )
        }
    },
    {
        field: 'propagation',
        headerName: 'Propagation Prediction',
        width: 220,
        type: 'number',
        headerAlign: 'left',
        align: 'left',
        valueGetter: (params: GridValueGetterParams) => {
            const prob = params.row.propagation_probability;
            return (prob !== undefined && prob !== null) ? prob : -1;
        },
        renderCell: (params: GridCellParams) => {
            const probability = params.row.propagation_probability as number | undefined;
            const snr = params.row.propagation_snr as number | undefined;
            const support = params.row.propagation_support as number | undefined;
            const mode = params.row.mode as string | undefined;

            const probabilityPct = (probability !== undefined && probability !== null)
                ? probability * 100
                : null;

            let color = '#757575';
            if (probabilityPct !== null) {
                if (probabilityPct > 75) {
                    color = 'green';
                } else if (probabilityPct > 25) {
                    color = '#f9a825';
                } else {
                    color = 'red';
                }
            }

            const mm = probabilityPct !== null ? `${probabilityPct.toFixed(0)}%` : '--';
            const nn = (snr !== undefined && snr !== null) ? `${snr >= 0 ? '+' : ''}${snr.toFixed(1)}dB` : '--';
            const pp = (support !== undefined && support !== null) ? support.toFixed(1) : '--';
            const modeLabel = mode ? `${mode.toUpperCase()} ` : '';

            const circleStyle: React.CSSProperties = {
                width: 10,
                height: 10,
                borderRadius: '50%',
                backgroundColor: color,
                display: 'inline-block',
                flex: '0 0 auto'
            };

            return (
                <Tooltip title="Mode – radio mode · Probability – chance of success · SNR – projected receive level · Support – WSPR reports backing the estimate">
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 'bold', fontSize: '0.85rem', color: isDarkMode ? '#fff' : '#111' }}>
                        <span style={circleStyle} />
                        <span>{`${modeLabel}${mm} | SNR: ${nn} | ${pp}`}</span>
                    </span>
                </Tooltip>
            );
        }
    },
    {
        field: 'hunted', headerName: 'Hunted', width: 100,
        renderCell: (x) => {
            return (
                <HuntedCheckbox hunted={x.row.hunted} hunted_bands={x.row.hunted_bands} />
            )
        }
    },
    {
        field: 'sig', headerName: 'SIG', width: 100,
        renderCell: (x) => {
            return <>
                {x.row.spot_source == 'SOTA' && (
                    <LandscapeIcon color='secondary' />
                )}
                {x.row.spot_source == 'POTA' && (
                    <ParkIcon color='primary' />
                )}
                {x.row.spot_source == 'WWFF' && (
                    <Brightness3Icon color='success' />
                )}

                <span id="sig">{x.row.spot_source}</span>
            </>
        }
    }
];


const rows: SpotRow[] = [];


var currentSortFilter = { field: 'spotTime', sort: 'desc' as GridSortDirection };
var currentPageFilter = { pageSize: 25, page: 0, };

function CustomToolbar() {
    return (
        <GridToolbarContainer>
            <GridToolbarColumnsButton />
            <GridToolbarDensitySelector />
            <GridToolbarQuickFilter />
            <ScanButton />
        </GridToolbarContainer>
    );
}

export default function SpotViewer() {
    const [spots, setSpots] = React.useState(rows)
    const [sortModel, setSortModel] = React.useState<GridSortModel>([currentSortFilter]);
    const [pageModel, setPaginationModel] = React.useState<GridPaginationModel>(currentPageFilter);
    const [rowSelectionModel, setRowSelectionModel] = React.useState<any[]>([]);
    const [backdropOpen, setBackdropOpen] = React.useState(false);
    const { contextData, setData, qsyButtonId, setLastQsyBtnId } = useAppContext();
    const { config } = useConfigContext();
    const propEnabled = config.prop_enabled ?? true;
    const isDarkMode = contextData.themeMode === 'dark';
    const baseColumns = React.useMemo(() => createBaseColumns(isDarkMode), [isDarkMode]);
    const columnDefs = React.useMemo(() => {
        if (propEnabled) {
            return baseColumns;
        }
        return baseColumns.filter((col) => col.field !== 'propagation');
    }, [propEnabled, baseColumns]);
    const activeBandFilter = contextData.bandFilter ?? 0;
    const multipleBandsVisible = propEnabled && spots.length > 0 && activeBandFilter === 0;

    function getSpots() {
        // get the spots from the db
        // NOTE: this gets called from the python backend on a timer and when
        // a qso is logged
        setBackdropOpen(true);
        const spots = window.pywebview.api.get_spots()
        spots.then((r: string) => {
            var x = JSON.parse(r);
            setSpots(x);
            setBackdropOpen(false);
        });
    }


    function setWorking() {
        // get the spots from the python backend
        // show the spinner to keep user from interacting with spots that wont
        // update.
        setBackdropOpen(true);
    }

    // when [spots] are set, update regions
    React.useEffect(() => {
        // the backend will parse out the regions for pota and sota (US, CA, W7)
        // and we will just set the available regions directly in the context.
        // however, we still parse the current spots and pull out the
        // location specifier (US-GA, CA-ON) for POTA spots only

        if (contextData.locationFilter === "")
            contextData.locations = [];

        if (window.pywebview === undefined) {
            return;
        }

        const p = window.pywebview.api.get_seen_regions();
        p.then((x: string) => {
            let json = checkApiResponse(x, contextData, setData);
            if (json.success) {
                contextData.regions = json.seen_regions;
                setData(contextData);
            }
        });

        // console.log(spots);
        spots.map((spot) => {
            // random white screen. one time there was a null dereference here
            // turning the hunterlog screen white
            if (spot === null)
                return;
            if (spot.spot_source == 'POTA') {
                let location = spot.locationDesc.substring(0, 5);
                if (!contextData.locations.includes(location))
                    contextData.locations.push(location);
            }
        });
        contextData.locations.sort();
    }, [spots]);

    React.useEffect(() => {
        if (window.pywebview !== undefined && window.pywebview.api !== null)
            initSpots();
        else
            window.addEventListener('pywebviewready', initSpots);

        function initSpots() {
            if (!window.pywebview.state) {
                window.pywebview.state = {}
            }

            // first run thru do this:
            getSpots();

            window.pywebview.state.getSpots = getSpots;
            window.pywebview.state.setWorking = setWorking;
        }

        try {
            let j = window.localStorage.getItem("SORT_MODEL") || '';
            let sm = JSON.parse(j) as GridSortModel;
            setSortModel(sm);
        } catch {
            console.log("ignored error loading sortmodel. using default");
        }

        try {
            let j = window.localStorage.getItem("PAGE_MODEL") || '';
            let pm = JSON.parse(j) as GridPaginationModel;
            console.log(`pagemodel ${j} ${pm}`)
            setPaginationModel(pm);
        } catch {
            console.log("ignored error loading pagination model. using default");
        }
    }, []);

    React.useEffect(() => {
        // get the spots from the db
        if (window.pywebview !== undefined) {
            getSpots();
        }
    },
        [contextData.bandFilter, contextData.regionFilter,
        contextData.qrtFilter, contextData.locationFilter,
        contextData.huntedFilter, contextData.onlyNewFilter,
        contextData.continentFilter, contextData.probabilityFilter]
    );

    // return the correct PK id for our rows
    function getRowId(row: { spotId: any; }) {
        return row.spotId;
    }

    const handleRowClick: GridEventListener<'rowClick'> = (
        params,  // GridRowParams
        event,   // MuiEvent<React.MouseEvent<HTMLElement>>
        details, // GridCallbackDetails
    ) => {
        // setting spotId in ctx is connected to HandleSpotRowClick
        const newCtxData = { ...contextData };
        // console.log('setting spot to ' + params.row.spotId);
        newCtxData.spotId = params.row.spotId;
        setData(newCtxData);

        // Also update visual selection to highlight the clicked row
        setRowSelectionModel([params.row.spotId]);
    };

    function setFilterModel(e: GridFilterModel) {
        contextData.filter = e;
        setData(contextData);
    };

    function setSortModelAndSave(newModel: GridSortModel) {
        setSortModel(newModel);
        window.localStorage.setItem("SORT_MODEL", JSON.stringify(newModel));
    }

    function setPaginationModelAndSave(newModel: GridPaginationModel) {
        setPaginationModel(newModel);
        window.localStorage.setItem("PAGE_MODEL", JSON.stringify(newModel));
    }

    function getClassName(params: GridRowClassNameParams<SpotRow>) {
        let highlightNewStr = window.localStorage.getItem("HIGHLIGHT_NEW_REF") || '1';
        let highlightNew = parseInt(highlightNewStr);

        if (params.row.is_qrt)
            return 'spotviewer-row-qrt';
        else if (params.row.park_hunts === 0 && highlightNew)
            return 'spotviewer-row-new';
        else
            return 'spotviewer-row';
    };



    return (
        <div className='spots-container'>
            <Backdrop
                sx={{ color: '#fff', zIndex: 1500 }}
                open={backdropOpen}
            >
                <CircularProgress color="inherit" />
            </Backdrop>

            {multipleBandsVisible && (
                <Alert severity="warning" sx={{ mb: 1 }}>
                    Propagation estimates only work when a single band is selected. Choose a band in the filter bar to enable predictions.
                </Alert>
            )}

            <DataGrid
                rows={spots}
                sx={{
                    "& .Mui-selected.spotviewer-row-new": {
                        backgroundColor: "rgba(75, 30, 110, 0.75) !important"
                    },
                    '& .Mui-selected': {
                        color: 'alert.main',
                    },
                }}
                slots={{ toolbar: CustomToolbar }}
                columns={columnDefs}
                getRowId={getRowId}
                initialState={{
                    pagination: {
                        paginationModel: pageModel,
                    },
                }}
                pageSizeOptions={[5, 10, 25, 100]}
                filterModel={contextData.filter}
                onFilterModelChange={(v) => setFilterModel(v)}
                onRowClick={handleRowClick}
                sortModel={sortModel}
                paginationModel={pageModel}
                onSortModelChange={(e) => setSortModelAndSave(e)}
                onPaginationModelChange={(e) => setPaginationModelAndSave(e)}
                getRowClassName={getClassName}
                rowSelectionModel={rowSelectionModel}
                onRowSelectionModelChange={(newSelection) => setRowSelectionModel(newSelection)}
            />
            <HandleSpotRowClick />
        </div>
    );
}
