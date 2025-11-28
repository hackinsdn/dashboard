/* global Chart:false */

$(function () {
  'use strict'

  /* ChartJS
   * -------
   * Here we will create a few charts using ChartJS
   */

  /* Leaflet Map
   * ------------
   * Create a world map with markers using parametrized config
   */
  
  var mapElement = document.getElementById('map');
  if (mapElement && mapElement.dataset.centerLat) {
    var centerLat = parseFloat(mapElement.dataset.centerLat);
    var centerLng = parseFloat(mapElement.dataset.centerLng);
    var zoomLevel = parseInt(mapElement.dataset.zoom);
    
    var map = L.map('map').setView([centerLat, centerLng], zoomLevel);
    
    var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);
    
    try {
      var mapPoints = window.mapConfig ? window.mapConfig.points : [];
      
      if (Array.isArray(mapPoints) && mapPoints.length > 0) {
        mapPoints.forEach(function(point) {
          if (point.lat && point.lng) {
            var marker = L.marker([point.lat, point.lng]).addTo(map);
            
            var iconClass = 'fas fa-map-marker-alt';
            var iconColor = 'blue';
            
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
            }
            
            marker.bindPopup(
              '<div>' +
                '<h6><i class="' + iconClass + '" style="color: ' + iconColor + '"></i> ' + point.name + '</h6>' +
                '<p>' + point.description + '</p>' +
              '</div>'
            );
          }
        });
      }
    } catch (error) {
      console.error('Error loading map points:', error);
    }
    
    if (typeof statesData !== 'undefined') {
      L.geoJson(statesData).addTo(map);
    }
    
    var updateMap = function() {
      var request = $.ajax({
        url: "/api/nodes",
        type: "GET",
        dataType: "json",
        contentType: "application/json",
      });
      request.done(function(data) {
        $.each(data.result, function(name, attrs) {
          L.marker([attrs.latitude, attrs.longitude]).addTo(map).bindPopup(attrs.tooltip);
        });
      });
    };
    setTimeout(updateMap, 50);
    
  } else {
    var map = L.map('map').setView([-15.13, -53.19], 4);
    var tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);

    if (typeof statesData !== 'undefined') {
      L.geoJson(statesData).addTo(map);
    }
    
    var updateMap = function() {
      var request = $.ajax({
        url: "/api/nodes",
        type: "GET",
        dataType: "json",
        contentType: "application/json",
      });
      request.done(function(data) {
        $.each(data.result, function(name, attrs) {
          L.marker([attrs.latitude, attrs.longitude]).addTo(map).bindPopup(attrs.tooltip);
        });
      });
    };
    setTimeout(updateMap, 50);
  }
})
