# Map Configuration

This document explains how to configure and customize the interactive map displayed on the dashboard.

## Initial Setup

Map configurations are defined through environment variables in the `.env` file:

### Configuration Variables

```bash
# Map configurations
export MAP_GEOJSON_JS="globe.js"    # GeoJSON javascript file (see below)
export MAP_CENTER_LAT=0             # Map center latitude
export MAP_CENTER_LNG=0             # Map center longitude
export MAP_ZOOM_LEVEL=1             # Initial zoom level (1-19)

# Map points (JSON format)
export MAP_POINTS='[...]'           # JSON array with points to be displayed
```

## Map GeoJSON

GeoJSON is an open standard format for encoding various geographic data structures using JSON (JavaScript Object Notation). It represents simple geographical features, along with their non-spatial attributes. Leaflet GeoJSON refers to the use of the GeoJSON data format within the Leaflet JavaScript library for creating interactive web maps.

In order to load GeoJSON according to the configuration in apps/config.py, you will need first to download geojson data, then convert it to JS and then change the configuration.

we provide the script convert-geojson-js.py to help on the conversion. Here is an example for Brazilian states:
```
git clone https://github.com/giuliano-macedo/geodata-br-states /tmp/geodata-br-states
for file in $(ls -1 /tmp/geodata-br-states/geojson/br_states); do jsfile=$(echo $file | sed s/json/js/g); python3 scripts/leaflet-geojson-js/convert-geojson-js.py /tmp/geodata-br-states/geojson/br_states/$file apps/static/assets/plugins/leaflet/geojson/$jsfile; done
```

Then you just need to change the `apps/config.py`:
```
    MAP_GEOJSON_JS = "Brasil.min.js"
```

You can also modify your .env file with the proper configs.

Here is an example for Brazilian states (using .env file):
```
export MAP_GEOJSON_JS="Brasil.min.js"
export MAP_CENTER_LAT="-15.13"
export MAP_CENTER_LNG="-53.19"
export MAP_ZOOM_LEVEL="4"
```

Another example for Bahia (Brazilian state):
```
export MAP_GEOJSON_JS="br_ba.js"
export MAP_CENTER_LAT="-13.57"
export MAP_CENTER_LNG="-41.50"
export MAP_ZOOM_LEVEL="6"
```

You can download geojson maps from this project: https://geojson-maps.kyd.au

Or you can search for other projects that have geojson data for you location.

## Map Points Configuration

### Point Structure

Each point on the map must follow this JSON structure:

```json
{
  "name": "Location name",
  "lat": -13.0073,
  "lng": -38.5107,
  "type": "university",
  "description": "Detailed location description"
}
```

### Required Fields

- **name**: Location name that will appear in the popup
- **lat**: Point latitude (decimal number)
- **lng**: Point longitude (decimal number)
- **type**: Point type/category (determines icon and color)
- **description**: Description that appears in the marker popup

### Available Point Types

The `type` field determines the marker icon and color:

| Type | Icon | Color | Usage |
|------|------|-------|-------|
| `university` | `fas fa-university` | Green | Universities |
| `institute` | `fas fa-school` | Orange | Institutes |
| `datacenter` | `fas fa-server` | Red | Datacenters |
| *others* | `fas fa-map-marker-alt` | Blue | Default |

## Customization

### Adding New Points

1. Edit the `.env` file
2. Locate the `MAP_POINTS` variable
3. Add a new JSON object to the array:

```bash
export MAP_POINTS='[
  {
    "name": "UFBA - Campus Ondina",
    "lat": -13.0073,
    "lng": -38.5107,
    "type": "university",
    "description": "Federal University of Bahia - Ondina Campus"
  },
  {
    "name": "New Location",
    "lat": -12.9000,
    "lng": -38.4000,
    "type": "datacenter",
    "description": "New location description"
  }
]'
```

### Creating New Point Types

To add new point types, edit the `apps/static/assets/js/pages/dashboard2.js` file:

```javascript
switch(point.type) {
  case 'university':
    iconClass = 'fas fa-university';
    iconColor = 'green';
    break;
  case 'institute':
    iconClass = 'fas fa-school';
    iconColor = 'orange';
    break;
  case 'datacenter':
    iconClass = 'fas fa-server';
    iconColor = 'red';
    break;
  case 'hospital':  // New type
    iconClass = 'fas fa-hospital';
    iconColor = 'blue';
    break;
  default:
    iconClass = 'fas fa-map-marker-alt';
    iconColor = 'blue';
}
```

### Changing Initial Position and Zoom

Modify the variables in the `.env` file:

```bash
# To center on São Paulo, for example:
export MAP_CENTER_LAT=-23.5505
export MAP_CENTER_LNG=-46.6333
export MAP_ZOOM_LEVEL=12
```

### Zoom Levels

- **1-3**: World/continental view
- **4-6**: Country view
- **7-10**: State/region view
- **11-13**: City view
- **14-16**: Neighborhood view
- **17-19**: Street view

## Complete Example

```bash
# Map configurations
export MAP_CENTER_LAT=-15.7801
export MAP_CENTER_LNG=-47.9292
export MAP_ZOOM_LEVEL=6

# Map points
export MAP_POINTS='[
  {
    "name": "UnB - University of Brasília",
    "lat": -15.7633,
    "lng": -47.8682,
    "type": "university",
    "description": "University of Brasília - Darcy Ribeiro Campus"
  },
  {
    "name": "IFSP - São Paulo",
    "lat": -23.5505,
    "lng": -46.6333,
    "type": "institute",
    "description": "Federal Institute of São Paulo"
  },
  {
    "name": "RNP Datacenter Rio",
    "lat": -22.9068,
    "lng": -43.1729,
    "type": "datacenter",
    "description": "RNP Datacenter in Rio de Janeiro"
  }
]'
```

## Applying Changes

After modifying the `.env` file:

1. Restart the Flask application
2. Reload the dashboard page
3. The map will be updated with the new configurations

## Tips

- Use precise coordinates for better positioning
- Keep descriptions concise but informative
- Test different zoom levels to find the best visualization
- Validate JSON before applying changes (use online tools like jsonlint.com)
- Keep backup of configurations before making changes

## Troubleshooting

### Map doesn't load
- Check if all variables are defined in `.env`
- Confirm that the points JSON is valid

### Points don't appear
- Check coordinates (latitude and longitude)
- Confirm that JSON is well formatted
- Check for unescaped special characters

### Icons don't appear
- Check if the type is correct
- Confirm that FontAwesome icons are loaded
