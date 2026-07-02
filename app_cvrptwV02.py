import requests
import math
import pandas as pd
import streamlit as st
import folium
from folium.plugins import AntPath
from streamlit_folium import st_folium
from solver import solve_cvrptw

# -------------------------------
# Session state initialization
# -------------------------------
if "optimization_result" not in st.session_state:
    st.session_state.optimization_result = None
if "optimization_data" not in st.session_state:
    st.session_state.optimization_data = None
if "optimization_metrics" not in st.session_state:
    st.session_state.optimization_metrics = None

# --- PAGE CONFIG ---
st.set_page_config(page_title="CV XX Shoes Distribution DSS", layout="wide", page_icon="👟")

# --- HEADER ---
st.title("Shoes Distribution Route Optimization System")
st.markdown("**Case Study:** CV XX CVRPTW Model (Updated Locations)")

# -------------------------------
# OSRM helpers
# -------------------------------
def get_osrm_matrices(data):
    coord_string = ";".join([f"{lon},{lat}" for lat, lon in data["raw_coords"]])
    url = f"http://router.project-osrm.org/table/v1/driving/{coord_string}?annotations=duration,distance"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    result = response.json()
    
    if result.get("code") != "Ok":
        raise ValueError(f"OSRM returned an error: {result}")
    
    data["distance_matrix"] = [[int(v) if v is not None else 999999999 for v in row] for row in result["distances"]]
    data["time_matrix"] = [[math.ceil(v/60) if v is not None else 999999 for v in row] for row in result["durations"]]
    return data

@st.cache_data(show_spinner=False)
def get_osrm_route_geometry(start_coord, end_coord):
    start_lat, start_lon = start_coord
    end_lat, end_lon = end_coord
    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}?overview=full&geometries=geojson&steps=false"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    result = response.json()
    if result.get("code") != "Ok":
        return [start_coord, end_coord], 0
    geometry = result["routes"][0]["geometry"]["coordinates"]
    return [(lat, lon) for lon, lat in geometry], None

def get_full_osrm_route(route):
    all_points = []
    for i in range(len(route["Coordinates"]) - 1):
        segment_points, _ = get_osrm_route_geometry(route["Coordinates"][i], route["Coordinates"][i+1])
        if i > 0 and len(segment_points) > 0:
            segment_points = segment_points[1:]
        all_points.extend(segment_points)
    return all_points, None

def create_enhanced_route_map(routes):
    route_colors = ["#FF0000", "#0000FF", "#008000", "#FFA500", "#800080", "#FF00FF", "#00FFFF"]
    
    valid_routes = [r for r in routes if r.get("Coordinates") and len(r["Coordinates"]) > 1]
    if not valid_routes:
        fallback_map = folium.Map(location=[-6.60315, 106.76218], zoom_start=11)
        return fallback_map

    combined_map = folium.Map(location=valid_routes[0]["Coordinates"][0], zoom_start=11)
    for route in valid_routes:
        vehicle_color = route_colors[(route["Vehicle"]-1) % len(route_colors)]
        road_points, _ = get_full_osrm_route(route)
        if road_points:
            AntPath(locations=road_points, color=vehicle_color, weight=6, opacity=0.9, delay=800).add_to(combined_map)
        
        for idx, stop in enumerate(route["Schedule"]):
            tw_start = stop.get('Opening_Minutes', 0)
            tw_end = stop.get('Deadline_Minutes', 1439)
            tw_text = f"{tw_start//60:02d}:{tw_start%60:02d} - {tw_end//60:02d}:{tw_end%60:02d}"

            folium.Marker(
                location=[stop["Latitude"], stop["Longitude"]],
                tooltip=f"Vehicle {route['Vehicle']} - Stop {idx+1}",
                popup=(
                    f"<b>{stop['Location']}</b><br>"
                    f"Operational Window: {tw_text}<br>"
                    f"Arrival Time: {stop.get('Time','N/A')}<br>"
                    f"Demand: {stop.get('Demand',0)}<br>"
                    f"Vehicle Utilization: {route.get('Utilization (%)',0)}%<br>"
                    f"Distance: {route.get('Distance (km)',0)} km<br>"
                    f"Lateness: {stop.get('Lateness_Minutes', 0)} min"
                ),
                icon=folium.Icon(color="blue" if idx > 0 and idx < len(route["Schedule"])-1 else "red", icon="info-sign")
            ).add_to(combined_map)
    return combined_map

def compute_vehicle_metrics(result, data):
    name_to_index = {name: idx for idx, name in enumerate(data["address_list"])}
    
    for route in result["route_results"]:
        schedule = route["Schedule"]
        delivered = sum(stop.get("Demand", 0) for stop in schedule)
        route_capacity = data["vehicle_capacities"][route["Vehicle"]-1]
        route["Utilization (%)"] = round((delivered / route_capacity) * 100, 1) if route_capacity > 0 else 0
        
        route_distance_m = 0
        for i in range(len(schedule) - 1):
            from_idx = name_to_index[schedule[i]["Location"]]
            to_idx = name_to_index[schedule[i+1]["Location"]]
            route_distance_m += data["distance_matrix"][from_idx][to_idx]
        route["Distance (km)"] = round(route_distance_m / 1000, 2)
        
        if schedule:
            total_lateness = 0
            for stop in schedule:
                arrival = stop.get("Arrival_Minutes", 0)
                deadline = stop.get("Deadline_Minutes", 1439)
                lateness = max(0, arrival - deadline)
                stop["Lateness_Minutes"] = lateness
                total_lateness += lateness
            route["Lateness (min)"] = total_lateness
        else:
            route["Lateness (min)"] = 0
            
    return result

def parse_time_to_minutes(time_str):
    try:
        parts = time_str.strip().split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return (hours * 60) + minutes
    except:
        return 0

# -------------------------------
# Sidebar inputs (Global parameters)
# -------------------------------
st.sidebar.header("⚙️ Operational Scenarios")
scenario = st.sidebar.selectbox("Select Scenario", ["Normal distribution day", "Peak distribution day", "Delayed departure"])

st.sidebar.header("🚚 Fleet Parameters")
fuel_cost_per_km = st.sidebar.number_input("Fuel Cost per KM", 1000, 50000, 5000)
driver_cost_per_vehicle = st.sidebar.number_input("Driver Cost per Vehicle", 10000, 500000, 50000)
num_vehicles = st.sidebar.number_input("Number of Vehicles", 1, 15, 5)
vehicle_capacity = st.sidebar.number_input("Vehicle Capacity (Cartons/Pairs)", 50, 10000, 2500)

# -------------------------------
# Dynamic Sidebar Location Controls (10:00 - 22:00 default for customers)
# -------------------------------
st.sidebar.header("📦 Location Custom Configurations")

locations_metadata = [
    {"name": "Depot", "def_demand": 0, "def_open": "00:00", "def_close": "23:59"},
    {"name": "Tangerang", "def_demand": 2000, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Pejaten", "def_demand": 220, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Central Park", "def_demand": 195, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Cikarang", "def_demand": 100, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Karawaci", "def_demand": 80, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Cibinong", "def_demand": 55, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Cibubur", "def_demand": 80, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Pondok Indah Mall 2", "def_demand": 80, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Casablanca", "def_demand": 95, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Alam Sutera", "def_demand": 130, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Depok", "def_demand": 100, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Sudirman", "def_demand": 110, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Plaza Indonesia", "def_demand": 200, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Bintaro", "def_demand": 170, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Bogor", "def_demand": 1000, "def_open": "10:00", "def_close": "22:00"},
    {"name": "Ciomas", "def_demand": 400, "def_open": "10:00", "def_close": "22:00"}
]

user_demands = []
user_time_windows = []

for loc in locations_metadata:
    with st.sidebar.expander(f"📍 {loc['name']}", expanded=False):
        if loc["name"] != "Depot":
            demand_input = st.number_input(f"Demand", 0, 5000, loc["def_demand"], key=f"d_in_{loc['name']}")
        else:
            demand_input = 0
            st.caption("Depot load defaults to 0.")
            
        col_start, col_end = st.columns(2)
        open_time_str = col_start.text_input("Open (HH:MM)", loc["def_open"], key=f"o_tm_{loc['name']}")
        close_time_str = col_end.text_input("Close (HH:MM)", loc["def_close"], key=f"c_tm_{loc['name']}")
        
        start_minutes = parse_time_to_minutes(open_time_str)
        end_minutes = parse_time_to_minutes(close_time_str)
        
        if start_minutes > end_minutes:
            st.error("Opening time cannot be later than closing time.")
            end_minutes = start_minutes
            
        user_demands.append(demand_input)
        user_time_windows.append((start_minutes, end_minutes))

# -------------------------------
# Run solver
# -------------------------------
if st.button("🚀 Run Route Optimization"):
    multiplier = 1.0
    if scenario == "Peak distribution day":
        multiplier = 1.25

    final_demands = [math.ceil(d * multiplier) if idx != 0 else 0 for idx, d in enumerate(user_demands)]

    data = {
        "address_list": [l["name"] for l in locations_metadata],
        "raw_coords": [
            (-6.60315, 106.76218),   # Depot
            (-6.3353364, 106.68034), # Tangerang
            (-6.286292, 106.81204),  # Pejaten
            (-6.171083, 106.787784), # Central Park
            (-6.333997, 107.13689),  # Cikarang
            (-6.225614, 106.628867), # Karawaci
            (-6.484245, 106.84319),  # Cibinong
            (-6.375656, 106.90173),  # Cibubur
            (-6.268711, 106.783856), # Pondok Indah Mall 2
            (-6.176046, 106.721175), # Casablanca
            (-6.237069, 106.65915),  # Alam Sutera
            (-6.380091, 106.84468),  # Depok
            (-6.224799, 106.80397),  # Sudirman
            (-6.194143, 106.82254),  # Plaza Indonesia
            (-6.285583, 106.72799),  # Bintaro
            (-6.616831, 106.82188),  # Bogor
            (-6.6013858, 106.75367)  # Ciomas
        ],
        "demands": final_demands,
        "vehicle_capacities": [vehicle_capacity] * num_vehicles,
        "num_vehicles": num_vehicles,
        "depot": 0,
        "depot_start": 0,              
        "time_windows": user_time_windows, 
        "service_times": [0, 6, 6, 6, 3, 6, 6, 3, 5, 5, 6, 6, 4, 6, 6, 3, 3], 
        "fuel_cost_per_km": fuel_cost_per_km,
        "driver_cost_per_vehicle": driver_cost_per_vehicle
    }

    with st.spinner("Fetching matrix configurations and solving..."):
        data = get_osrm_matrices(data)
        result = solve_cvrptw(data)
        
    if result is None:
        st.error("No feasible solution found with current configurations. Try increasing Vehicle Capacity, reducing demands, or widening time windows.")
        st.stop()

    for route in result["route_results"]:
        for stop in route["Schedule"]:
            loc_name = stop["Location"]
            idx = data["address_list"].index(loc_name)
            stop["Opening_Minutes"] = data["time_windows"][idx][0]

    st.session_state.optimization_result = compute_vehicle_metrics(result, data)
    st.session_state.optimization_data = data

# -------------------------------
# Display results
# -------------------------------
if st.session_state.optimization_result:
    result = st.session_state.optimization_result
    data = st.session_state.optimization_data
    routes = result["route_results"]

    # --- DYNAMIC BASELINE CALCULATION ---
    total_unoptimized_meters = 0
    matrix = data["distance_matrix"]
    for i in range(1, len(data["address_list"])):
        total_unoptimized_meters += matrix[0][i] + matrix[i][0]
    
    baseline_distance = round(total_unoptimized_meters / 1000, 2)
    optimized_distance = sum(r.get("Distance (km)", 0) for r in routes)
    improvement = ((baseline_distance - optimized_distance) / baseline_distance) * 100 if baseline_distance > 0 else 0
    
    col1, col2, col3, col4, col5 = st.columns(5)
    total_operational_cost = sum(r.get("Total Cost", 0) for r in routes)
    total_packages = sum(r.get('Delivered Packages', 0) for r in routes)
    total_capacity = sum(data['vehicle_capacities'])
    
    col1.metric("Baseline Distance", f"{baseline_distance:.2f} km")
    col2.metric("Optimized Distance", f"{optimized_distance:.2f} km", f"{improvement:.2f}% improvement")
    col3.metric("Total Dispatched", f"{total_packages} items")
    col4.metric("Fleet Utilization", f"{(total_packages / total_capacity * 100) if total_capacity > 0 else 0:.1f}%")
    col5.metric("Total Operational Cost", f"Rp {total_operational_cost:,.0f}")
    
    # --- Combined map ---
    st.subheader("🗺️ Combined Route Map")
    st_folium(create_enhanced_route_map(routes), width=1000, height=500)

    # --- Vehicle summary table ---
    st.subheader("🚛 Vehicle Summary Table")
    
    for r in routes:
        for col_name in ["Fuel Cost", "Driver Cost", "Total Cost"]:
            if col_name not in r:
                r[col_name] = 0
                
    vehicle_summary_df = pd.DataFrame(routes)[[
        "Vehicle",
        "Distance (km)",
        "Delivered Packages",
        "Utilization (%)",
        "Fuel Cost",
        "Driver Cost",
        "Total Cost",
        "Lateness (min)"
    ]]
    
    st.dataframe(vehicle_summary_df, use_container_width=True)

    # --- Per-vehicle sections ---
    for route in routes:
        st.markdown(f"### Vehicle {route['Vehicle']}")
        if route.get("Delivered Packages", 0) == 0 or not route.get("Schedule"):
            st.info("Vehicle not needed for this configuration.")
            continue

        with st.expander(f"Vehicle {route['Vehicle']} Map", expanded=False):
            st_folium(create_enhanced_route_map([route]), width=1000, height=450)

        schedule_records = []
        for s in route["Schedule"]:
            open_min = s.get("Opening_Minutes", 0)
            close_min = s.get("Deadline_Minutes", 1439)
            
            schedule_records.append({
                "Location": s["Location"],
                "Operational Window": f"{open_min//60:02d}:{open_min%60:02d} - {close_min//60:02d}:{close_min%60:02d}",
                "Arrival Time": s["Time"],
                "Demand": s["Demand"],
                "Lateness (min)": s.get("Lateness_Minutes", 0),
                "Latitude": s["Latitude"],
                "Longitude": s["Longitude"]
            })
            
        stop_df = pd.DataFrame(schedule_records)[["Location", "Operational Window", "Arrival Time", "Demand", "Lateness (min)", "Latitude", "Longitude"]]
        
        st.markdown(f"#### Stop-Level Delivery Table (Vehicle {route['Vehicle']})")
        st.dataframe(stop_df, use_container_width=True)

        csv_bytes = stop_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label=f"📥 Download CSV Vehicle {route['Vehicle']}", 
            data=csv_bytes,
            file_name=f"indrajaya_vehicle_{route['Vehicle']}_stops.csv", 
            mime="text/csv"
        )
